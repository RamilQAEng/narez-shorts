import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, Pencil, Plus } from 'lucide-react';
import { apiFetch } from '../lib/api';
import FrameBoxEditor from './FrameBoxEditor';

// Saved frame layouts (game box + webcam box + banner). The library lives on the server (/api/projects).
const KEY = 'os_project';

export default function ProjectPicker({ value, onChange, url, banner }) {
    const [projects, setProjects] = useState([]);
    const [editor, setEditor] = useState(null); // null | {project?}
    const [error, setError] = useState('');
    const onChangeRef = useRef(onChange);
    onChangeRef.current = onChange;

    const load = useCallback(async () => {
        try {
            const res = await apiFetch('/api/projects');
            if (!res.ok) return [];
            const list = (await res.json()).projects || [];
            setProjects(list);
            return list;
        } catch { return []; }
    }, []);

    const pick = useCallback((p) => {
        onChangeRef.current(p);
        try { localStorage.setItem(KEY, p ? p.id : ''); } catch { /* ignore */ }
    }, []);

    useEffect(() => {
        load().then((list) => {
            let saved = '';
            try { saved = localStorage.getItem(KEY) || ''; } catch { /* ignore */ }
            const p = list.find((x) => x.id === saved);
            if (p) pick(p);
        });
    }, [load, pick]);

    const remove = async (p) => {
        if (!window.confirm(`Удалить проект «${p.name}»?`)) return;
        const res = await apiFetch(`/api/projects/${p.id}`, { method: 'DELETE' });
        if (!res.ok) { setError('не удалось удалить'); return; }
        if (value === p.id) pick(null);
        load();
    };

    const chip = (active) => `flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-xs transition-colors
        ${active ? 'border-[color:var(--color-accent)] text-ink' : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'}`;

    return (
        <div>
            <p className="text-center font-mono text-[11px] tracking-widest uppercase text-muted mb-3">Проект</p>
            <div className="flex flex-wrap gap-2 justify-center">
                <button type="button" onClick={() => pick(null)} className={chip(!value)}>Без проекта</button>
                {projects.map((p) => (
                    <span key={p.id} className={chip(value === p.id)}>
                        <button type="button" onClick={() => pick(p)}>{p.name}</button>
                        <button type="button" onClick={() => setEditor({ project: p })} aria-label="изменить проект" className="hover:text-ink"><Pencil size={12} /></button>
                        <button type="button" onClick={() => remove(p)} aria-label="удалить проект" className="hover:text-ink"><X size={12} /></button>
                    </span>
                ))}
                <button type="button" onClick={() => setEditor({})} className={`${chip(false)} border-dashed`}><Plus size={12} /> новый проект</button>
            </div>
            {error && <p className="mt-2 text-xs text-warn text-center">{error}</p>}
            {editor && (
                <FrameBoxEditor project={editor.project} url={url} banner={banner}
                                onClose={() => setEditor(null)}
                                onSaved={async (p) => { setEditor(null); await load(); pick(p); }} />
            )}
        </div>
    );
}
