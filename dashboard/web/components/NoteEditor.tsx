'use client';

import { useState, useEffect } from 'react';
import { api } from '@/lib/api';

interface NoteEditorProps {
  date: string;
  initialText?: string;
}

export default function NoteEditor({ date, initialText = '' }: NoteEditorProps) {
  const [text, setText] = useState(initialText);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setText(initialText);
  }, [initialText]);

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      await api.saveNote(date, text);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      // ignore
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 space-y-3 border border-surface-border">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
          Notes
        </h3>
        <span className="text-xs text-gray-600">{date}</span>
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Add notes about last night's sleep..."
        rows={3}
        className="
          w-full bg-surface-raised rounded-xl px-4 py-3
          text-sm text-white placeholder-gray-600
          border border-surface-border
          focus:outline-none focus:border-brand
          resize-none
        "
      />
      <button
        onClick={handleSave}
        disabled={saving}
        className="
          w-full min-h-[44px] rounded-xl font-semibold text-sm
          transition-all
          disabled:opacity-40
          flex items-center justify-center gap-2
          bg-brand hover:bg-brand-dark text-white
        "
      >
        {saving ? (
          <>
            <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Saving…
          </>
        ) : saved ? (
          'Saved!'
        ) : (
          'Save Note'
        )}
      </button>
    </div>
  );
}
