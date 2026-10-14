import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { ImagePlus } from 'lucide-react';
import { MAX_IMAGES, bodyParts, imageMarker, type ImageInput } from '../shared/images';
import type { Draft } from './Composer';
import { compressImage } from './image-files';

// The editable surface contains only text, line breaks and our own image figures.
function readBody(root: Node): string {
  if (root.nodeType === Node.TEXT_NODE) return root.textContent || '';
  if (!(root instanceof HTMLElement)) return '';
  if (root.dataset.imageId) return imageMarker(root.dataset.imageId);
  if (root.tagName === 'BR') return '\n';
  let text = '';
  for (const child of Array.from(root.childNodes)) {
    const block = child instanceof HTMLElement && ['DIV', 'P'].includes(child.tagName);
    if (block && text && !text.endsWith('\n')) text += '\n';
    text += readBody(child);
    if (block && child.nextSibling && !text.endsWith('\n')) text += '\n';
  }
  return text;
}
function figure(img: ImageInput) {
  const node = document.createElement('figure');
  node.contentEditable = 'false';
  node.dataset.imageId = img.id;
  node.className = 'editor-image';
  const picture = document.createElement('img');
  picture.src = `data:${img.contentType};base64,${img.content}`;
  picture.alt = img.name;
  picture.draggable = false;
  const caption = document.createElement('figcaption');
  caption.textContent = img.name;
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.textContent = '删除图片';
  remove.dataset.removeImage = img.id;
  remove.setAttribute('aria-label', '删除图片 ' + img.name);
  for (const child of [picture, caption, remove]) node.appendChild(child);
  return node;
}
export default function LetterEditor({
  draft,
  onChange,
  disabled,
  onProcessing,
}: {
  draft: Draft;
  onChange: Dispatch<SetStateAction<Draft>>;
  disabled: boolean;
  onProcessing: (value: boolean) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const caret = useRef<Range | null>(null);
  const current = useRef(draft);
  current.current = draft;
  const generation = useRef(0);
  const working = useRef(false);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const el = root.current!;
    const ids = Array.from(el.querySelectorAll<HTMLElement>('[data-image-id]')).map(
      (n) => n.dataset.imageId,
    );
    if (readBody(el) === draft.body && draft.images.every((img) => ids.includes(img.id))) return;
    el.replaceChildren();
    for (const part of bodyParts(draft.body)) {
      if ('text' in part) el.appendChild(document.createTextNode(part.text));
      else {
        const img = draft.images.find((i) => i.id === part.imageId);
        if (img) el.appendChild(figure(img));
      }
    }
    caret.current = null;
    generation.current++;
  }, [draft.body, draft.images]);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  const remember = () => {
    const selection = window.getSelection();
    if (
      selection?.rangeCount &&
      root.current?.contains(selection.getRangeAt(0).commonAncestorContainer)
    )
      caret.current = selection.getRangeAt(0).cloneRange();
  };
  const sync = () => {
    const body = readBody(root.current!);
    if (body.length > 20000) setError('正文超过 20,000 字，请删减后再寄出');
    else setError('');
    onChange((old) => ({
      ...old,
      body,
      images: old.images.filter((img) => body.includes(imageMarker(img.id))),
    }));
    remember();
  };
  const insertionRange = () => {
    const el = root.current!;
    const saved = caret.current;
    if (saved && el.contains(saved.commonAncestorContainer)) return saved;
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    return range;
  };
  const insertNodes = (nodes: Node[]) => {
    const range = insertionRange();
    range.deleteContents();
    const fragment = document.createDocumentFragment();
    fragment.append(...nodes);
    const last = nodes.at(-1)!;
    range.insertNode(fragment);
    range.setStartAfter(last);
    range.collapse(true);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    caret.current = range.cloneRange();
  };
  const addFiles = async (files: File[]) => {
    if (disabled || working.current || !files.length) return;
    remember();
    if (files.length + current.current.images.length > MAX_IMAGES) {
      setError('每封信最多插入 3 张图片');
      return;
    }
    const version = generation.current;
    working.current = true;
    setProcessing(true);
    onProcessing(true);
    setError('');
    try {
      const images: ImageInput[] = [];
      for (const file of files) images.push(await compressImage(file));
      if (generation.current !== version || !root.current) return;
      insertNodes(images.flatMap((img) => [figure(img), document.createTextNode('\n')]));
      const body = readBody(root.current);
      onChange((old) => ({
        ...old,
        body,
        images: [...old.images, ...images].filter((img) => body.includes(imageMarker(img.id))),
      }));
      requestAnimationFrame(() => {
        if (generation.current !== version || !root.current) return;
        root.current.focus();
        const selection = window.getSelection();
        if (caret.current) {
          selection?.removeAllRanges();
          selection?.addRange(caret.current);
        }
      });
    } catch (e) {
      if (generation.current === version) setError((e as Error).message);
    } finally {
      working.current = false;
      setProcessing(false);
      onProcessing(false);
    }
  };
  return (
    <>
      <div className="image-toolbar">
        <button
          type="button"
          className="subtle-button"
          disabled={disabled || processing || draft.images.length >= MAX_IMAGES}
          onMouseDown={(e) => {
            e.preventDefault();
            remember();
          }}
          onClick={() => {
            remember();
            picker.current?.click();
          }}
        >
          <ImagePlus size={16} /> {processing ? '正在整理图片…' : '选择图片'}
        </button>
        <span>{draft.images.length} / 3 张 · 自动压缩</span>
        <input
          ref={picker}
          type="file"
          accept="image/jpeg,image/png,image/webp"
          multiple
          hidden
          aria-label="选择正文图片"
          onChange={(e) => {
            void addFiles(Array.from(e.target.files || []));
            e.target.value = '';
          }}
        />
      </div>
      <div
        ref={root}
        id="body"
        className="letter-editor"
        role="textbox"
        aria-label="想说的话"
        aria-multiline="true"
        aria-disabled={disabled || processing}
        contentEditable={!disabled && !processing}
        suppressContentEditableWarning
        data-placeholder={'嗨，\n\n今天有件小事，想和你分享……'}
        onInput={sync}
        onKeyUp={remember}
        onMouseUp={remember}
        onBlur={remember}
        onKeyDown={(e) => {
          if ((e.target as HTMLElement).closest('button')) return;
          if (disabled || working.current) return;
          remember();
          if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
            e.preventDefault();
            insertNodes([document.createTextNode('\n')]);
            sync();
          }
        }}
        onPaste={(e) => {
          e.preventDefault();
          if (disabled || working.current) return;
          const files = Array.from(e.clipboardData.files);
          if (files.length) {
            void addFiles(files);
            return;
          }
          const text = e.clipboardData.getData('text/plain');
          if (text) {
            remember();
            insertNodes([document.createTextNode(text)]);
            sync();
          } else setError('剪贴板里没有图片文件，请复制图片本身或使用“选择图片”');
        }}
        onDrop={(e) => {
          e.preventDefault();
          setError('请使用粘贴或“选择图片”插入图片');
        }}
        onClick={(e) => {
          const button = (e.target as HTMLElement).closest<HTMLElement>('[data-remove-image]');
          if (!button || disabled || processing) return;
          button.closest('figure')?.remove();
          caret.current = null;
          sync();
        }}
      />
      {error && (
        <p role="alert" className="image-error">
          {error}
        </p>
      )}
    </>
  );
}
