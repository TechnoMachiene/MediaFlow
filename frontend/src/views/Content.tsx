import { useMemo, useState } from 'react';
import { FIELD_LABELS, SECTION_LABELS, humanize, parseContent } from '../lib';
import { CopyButton, Segmented } from '../ui';

/** Optional review hooks: open-comment counts per field, and a way to start one. */
export interface FieldNotes {
  counts: Record<string, number>;
  onComment: (path: string) => void;
}

function NoteButton({ path, notes }: { path: string; notes?: FieldNotes }) {
  if (!notes) return null;
  const count = notes.counts[path] ?? 0;
  return (
    <button type="button" className="copy-btn" onClick={() => notes.onComment(path)}>
      <span>{count ? `${count} comment${count > 1 ? 's' : ''}` : 'Comment'}</span>
    </button>
  );
}

function ContentField({
  path, name, value, notes,
}: { path: string; name: string; value: unknown; notes?: FieldNotes }) {
  const label = FIELD_LABELS[name] ?? humanize(name);
  if (Array.isArray(value)) {
    const items = value.map(String).filter(Boolean);
    if (!items.length) return null;
    const ordered = name === 'on_screen_text';
    return (
      <section className="content-field">
        <header>
          <h4>{label}</h4>
          <NoteButton path={path} notes={notes} />
          <CopyButton text={items.join(ordered ? '\n' : ' ')} />
        </header>
        {ordered ? (
          <ol dir="auto">{items.map((item, i) => <li key={i}>{item}</li>)}</ol>
        ) : (
          <div className="chips" dir="auto">
            {items.map((item, i) => <span key={i} className="chip">{item}</span>)}
          </div>
        )}
      </section>
    );
  }
  const text = typeof value === 'string' ? value.trim() : value == null ? '' : String(value);
  if (!text) return null;
  return (
    <section className="content-field">
      <header>
        <h4>{label}</h4>
        <NoteButton path={path} notes={notes} />
        <CopyButton text={text} />
      </header>
      <p dir="auto">{text}</p>
    </section>
  );
}

export function ContentView({ text, notes }: { text: string; notes?: FieldNotes }) {
  const content = useMemo(() => parseContent(text), [text]);
  const sections = 'structured' in content ? Object.keys(content.structured) : [];
  const [tab, setTab] = useState(sections[0] ?? '');
  const current = sections.includes(tab) ? tab : sections[0];

  if ('raw' in content) {
    return (
      <section className="content-field">
        <header>
          <h4>Draft</h4>
          <NoteButton path="draft" notes={notes} />
          <CopyButton text={content.raw} />
        </header>
        <p dir="auto">{content.raw}</p>
      </section>
    );
  }
  const fields = Object.entries(content.structured[current]);
  return (
    <div className="content">
      {sections.length > 1 && (
        <Segmented
          label="Platform"
          value={current}
          onChange={setTab}
          options={sections.map((s) => ({ value: s, label: SECTION_LABELS[s] ?? humanize(s) }))}
        />
      )}
      {fields.map(([name, value]) => (
        <ContentField key={name} path={`${current}.${name}`} name={name} value={value} notes={notes} />
      ))}
    </div>
  );
}
