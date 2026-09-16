import type { editor as MonacoEditor } from 'monaco-editor';
import type { Awareness } from 'y-protocols/awareness';
import * as Y from 'yjs';

interface RelativeSelection {
  anchor: Y.RelativePosition;
  head: Y.RelativePosition;
}

interface Disposable {
  dispose: () => void;
}

/**
 * Minimal Y.Text/Monaco adapter that avoids y-monaco's runtime import of a
 * second complete Monaco build. Monaco accepts structural ranges/selections,
 * so only erased TypeScript types are imported here.
 */
export class YMonacoBinding {
  private readonly doc: Y.Doc;
  private readonly decorations = new Map<
    MonacoEditor.IStandaloneCodeEditor,
    string[]
  >();
  private readonly disposables: Disposable[] = [];
  private savedSelections = new Map<
    MonacoEditor.IStandaloneCodeEditor,
    RelativeSelection
  >();
  private applying = false;
  private destroyed = false;
  private readonly ytext: Y.Text;
  private readonly model: MonacoEditor.ITextModel;
  private readonly editors: Set<MonacoEditor.IStandaloneCodeEditor>;
  private readonly awareness: Awareness | null;

  constructor(
    ytext: Y.Text,
    model: MonacoEditor.ITextModel,
    editors: Set<MonacoEditor.IStandaloneCodeEditor>,
    awareness: Awareness | null = null,
  ) {
    this.ytext = ytext;
    this.model = model;
    this.editors = editors;
    this.awareness = awareness;
    if (!ytext.doc) throw new Error('Y.Text must be attached to a Y.Doc');
    this.doc = ytext.doc;
    this.doc.on('beforeAllTransactions', this.captureSelections);
    this.ytext.observe(this.applyYjsChanges);

    if (model.getValue() !== ytext.toString()) model.setValue(ytext.toString());

    this.disposables.push(
      model.onDidChangeContent((event) => {
        this.withMutex(() => {
          this.doc.transact(() => {
            [...event.changes]
              .sort((left, right) => right.rangeOffset - left.rangeOffset)
              .forEach((change) => {
                this.ytext.delete(change.rangeOffset, change.rangeLength);
                this.ytext.insert(change.rangeOffset, change.text);
              });
          }, this);
        });
      }),
      model.onWillDispose(() => this.destroy()),
    );

    if (awareness) {
      for (const editor of editors) {
        this.disposables.push(
          editor.onDidChangeCursorSelection((event) => {
            if (editor.getModel() !== model) return;
            const selection = event.selection;
            const anchor = model.getOffsetAt({
              lineNumber: selection.selectionStartLineNumber,
              column: selection.selectionStartColumn,
            });
            const head = model.getOffsetAt({
              lineNumber: selection.positionLineNumber,
              column: selection.positionColumn,
            });
            awareness.setLocalStateField('selection', {
              anchor: Y.createRelativePositionFromTypeIndex(ytext, anchor),
              head: Y.createRelativePositionFromTypeIndex(ytext, head),
            });
          }),
        );
      }
      awareness.on('change', this.renderDecorations);
    }
  }

  private withMutex(action: () => void) {
    if (this.applying) return;
    this.applying = true;
    try {
      action();
    } finally {
      this.applying = false;
    }
  }

  private readonly captureSelections = () => {
    this.withMutex(() => {
      this.savedSelections = new Map();
      for (const editor of this.editors) {
        if (editor.getModel() !== this.model) continue;
        const selection = editor.getSelection();
        if (!selection) continue;
        this.savedSelections.set(editor, {
          anchor: Y.createRelativePositionFromTypeIndex(
            this.ytext,
            this.model.getOffsetAt({
              lineNumber: selection.selectionStartLineNumber,
              column: selection.selectionStartColumn,
            }),
          ),
          head: Y.createRelativePositionFromTypeIndex(
            this.ytext,
            this.model.getOffsetAt({
              lineNumber: selection.positionLineNumber,
              column: selection.positionColumn,
            }),
          ),
        });
      }
    });
  };

  private readonly applyYjsChanges = (event: Y.YTextEvent) => {
    this.withMutex(() => {
      let index = 0;
      for (const operation of event.delta) {
        if (operation.retain !== undefined) {
          index += operation.retain;
        } else if (operation.insert !== undefined) {
          const position = this.model.getPositionAt(index);
          const text = String(operation.insert);
          this.model.applyEdits([{
            range: {
              startLineNumber: position.lineNumber,
              startColumn: position.column,
              endLineNumber: position.lineNumber,
              endColumn: position.column,
            },
            text,
          }]);
          index += text.length;
        } else if (operation.delete !== undefined) {
          const start = this.model.getPositionAt(index);
          const end = this.model.getPositionAt(index + operation.delete);
          this.model.applyEdits([{
            range: {
              startLineNumber: start.lineNumber,
              startColumn: start.column,
              endLineNumber: end.lineNumber,
              endColumn: end.column,
            },
            text: '',
          }]);
        }
      }

      for (const [editor, relative] of this.savedSelections) {
        const anchor = Y.createAbsolutePositionFromRelativePosition(
          relative.anchor,
          this.doc,
        );
        const head = Y.createAbsolutePositionFromRelativePosition(
          relative.head,
          this.doc,
        );
        if (!anchor || !head || anchor.type !== this.ytext || head.type !== this.ytext) {
          continue;
        }
        const anchorPosition = this.model.getPositionAt(anchor.index);
        const headPosition = this.model.getPositionAt(head.index);
        editor.setSelection({
          selectionStartLineNumber: anchorPosition.lineNumber,
          selectionStartColumn: anchorPosition.column,
          positionLineNumber: headPosition.lineNumber,
          positionColumn: headPosition.column,
        });
      }
    });
    this.renderDecorations();
  };

  private readonly renderDecorations = () => {
    if (!this.awareness) return;
    const styleRules: string[] = [];
    for (const editor of this.editors) {
      if (editor.getModel() !== this.model) continue;
      const next: MonacoEditor.IModelDeltaDecoration[] = [];
      this.awareness.getStates().forEach((state, clientId) => {
        if (clientId === this.doc.clientID || !state.selection) return;
        const anchor = Y.createAbsolutePositionFromRelativePosition(
          state.selection.anchor,
          this.doc,
        );
        const head = Y.createAbsolutePositionFromRelativePosition(
          state.selection.head,
          this.doc,
        );
        if (!anchor || !head || anchor.type !== this.ytext || head.type !== this.ytext) {
          return;
        }
        const anchorPosition = this.model.getPositionAt(anchor.index);
        const headPosition = this.model.getPositionAt(head.index);
        const start = anchor.index <= head.index ? anchorPosition : headPosition;
        const end = anchor.index <= head.index ? headPosition : anchorPosition;
        const headClass = `yRemoteSelectionHead-${clientId}`;
        next.push({
          range: {
            startLineNumber: start.lineNumber,
            startColumn: start.column,
            endLineNumber: end.lineNumber,
            endColumn: end.column,
          },
          options: {
            className: `yRemoteSelection yRemoteSelection-${clientId}`,
            afterContentClassName: anchor.index <= head.index ? headClass : undefined,
            beforeContentClassName: anchor.index > head.index ? headClass : undefined,
          },
        });
        const candidateColor = state.user?.color;
        const color = typeof candidateColor === 'string' &&
          /^#[0-9a-f]{6}$/i.test(candidateColor) ? candidateColor : '#7c8cff';
        styleRules.push(
          `.yRemoteSelection-${clientId}{background:${color}2f}`,
          `.${headClass}{border-left:2px solid ${color};height:1.25em}`,
        );
      });
      this.decorations.set(
        editor,
        editor.deltaDecorations(this.decorations.get(editor) || [], next),
      );
    }
    let style = document.getElementById('concord-remote-cursors');
    if (!style) {
      style = document.createElement('style');
      style.id = 'concord-remote-cursors';
      document.head.appendChild(style);
    }
    style.textContent = styleRules.join('\n');
  };

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.ytext.unobserve(this.applyYjsChanges);
    this.doc.off('beforeAllTransactions', this.captureSelections);
    if (this.awareness) this.awareness.off('change', this.renderDecorations);
    for (const disposable of this.disposables) disposable.dispose();
    for (const [editor, ids] of this.decorations) editor.deltaDecorations(ids, []);
    this.decorations.clear();
  }
}
