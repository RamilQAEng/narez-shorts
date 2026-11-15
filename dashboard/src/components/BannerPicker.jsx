import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Upload, X, Loader2 } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { getApiUrl } from '../config';

// Brand banner (image / GIF / video) laid over every clip of the job.
// The library lives on the server (/api/banners); id and place travel with the link.
const KEY = 'os_banner';

const PLACES = [
    { value: 'top', label: 'Сверху', hint: 'под строкой поиска приложения' },
    { value: 'bottom', label: 'Снизу', hint: 'над подписью и кнопками' },
];

function Thumb({ banner }) {
    const src = getApiUrl(`/api/banners/${banner.id}/file`);
    return banner.kind === 'video'
        ? <video src={src} muted loop autoPlay playsInline className="w-full h-full object-contain" />
        : <img src={src} alt={banner.name} className="w-full h-full object-contain" />;
}

// Phone outline with the banner strip where it will sit.
function PlaceGlyph({ where, active }) {
    return (
        <span className="relative block w-8 h-14 rounded-[5px] border-2 shrink-0 transition-colors"
              style={{ borderColor: active ? 'var(--color-accent)' : 'var(--color-rule-2)' }}>
            <span className="absolute left-[3px] right-[3px] h-2.5 rounded-[2px]"
                  style={{ [where]: 4, background: 'var(--color-accent)', opacity: active ? 1 : 0.5 }} />
        </span>
    );
}

export default function BannerPicker({ value, onChange }) {
    const [banners, setBanners] = useState([]);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const fileRef = useRef(null);
    const valueRef = useRef(value);
    valueRef.current = value;
    const onChangeRef = useRef(onChange);
    onChangeRef.current = onChange;

    const load = useCallback(async () => {
        try {
            const res = await apiFetch('/api/banners');
            if (res.ok) setBanners((await res.json()).banners || []);
        } catch { /* picker stays empty */ }
    }, []);

    useEffect(() => {
        load();
        try {
            const saved = JSON.parse(localStorage.getItem(KEY) || 'null');
            if (saved) onChangeRef.current({ id: saved.id || '', place: saved.place || 'top' });
        } catch { /* ignore */ }
    }, [load]);

    const set = (patch) => {
        const next = { ...valueRef.current, ...patch };
        onChangeRef.current(next);
        try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* ignore */ }
    };

    const upload = async (file) => {
        if (!file) return;
        setError('');
        setBusy(true);
        const form = new FormData();
        form.append('file', file);
        try {
            const res = await apiFetch('/api/banners', { method: 'POST', body: form });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'не удалось загрузить');
            const made = await res.json();
            await load();
            set({ id: made.id });
        } catch (e) {
            setError(e.message);
        } finally {
            setBusy(false);
        }
    };

    const remove = async (id) => {
        if (!window.confirm('Удалить этот баннер?')) return;
        const res = await apiFetch(`/api/banners/${id}`, { method: 'DELETE' });
        if (!res.ok) { setError('не удалось удалить'); return; }
        if (value.id === id) set({ id: '' });
        load();
    };

    const cardClass = (active) => `relative aspect-[16/9] rounded-input border overflow-hidden transition-colors
        ${active ? 'border-[color:var(--color-accent)]' : 'border-rule2 hover:border-[color:var(--color-accent)]'}`;

    return (
        <div>
            <p className="text-center font-mono text-[11px] tracking-widest uppercase text-muted mb-3">Баннер бренда</p>
            <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                <button type="button" onClick={() => set({ id: '' })} className={`${cardClass(!value.id)} flex items-center justify-center text-xs text-muted`}>
                    Без баннера
                </button>
                {banners.map((b) => (
                    <div key={b.id} className={`${cardClass(value.id === b.id)} bg-black/30`}>
                        <button type="button" onClick={() => set({ id: b.id })} className="absolute inset-0 p-1" aria-label={b.name} title={b.name}>
                            <Thumb banner={b} />
                        </button>
                        <button type="button" onClick={() => remove(b.id)} aria-label="удалить баннер"
                                className="absolute top-1 right-1 w-5 h-5 rounded-full bg-black/70 text-ink2 hover:text-ink flex items-center justify-center">
                            <X size={12} />
                        </button>
                    </div>
                ))}
                <button type="button" onClick={() => fileRef.current?.click()} disabled={busy}
                        className="aspect-[16/9] rounded-input border border-dashed border-rule2 hover:border-[color:var(--color-accent)] text-muted flex flex-col items-center justify-center gap-1 text-xs transition-colors">
                    {busy ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
                    {busy ? 'загрузка…' : 'загрузить'}
                    <span className="text-[10px] text-muted">png · jpg · gif · mp4</span>
                </button>
            </div>
            <input ref={fileRef} type="file" className="hidden"
                   accept=".png,.jpg,.jpeg,.webp,.gif,.mp4,.webm,.mov"
                   onChange={(e) => { upload(e.target.files[0]); e.target.value = ''; }} />
            {error && <p className="mt-2 text-xs text-warn">{error}</p>}

            {value.id && (
                <div className="grid grid-cols-2 gap-2 mt-3">
                    {PLACES.map((p) => {
                        const active = value.place === p.value;
                        return (
                            <button key={p.value} type="button" onClick={() => set({ place: p.value })}
                                    className={`py-2.5 px-3 rounded-input border flex items-center gap-3 text-left transition-colors
                                        ${active ? 'border-[color:var(--color-accent)] text-ink' : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'}`}>
                                <PlaceGlyph where={p.value} active={active} />
                                <span>
                                    <span className="block text-sm">{p.label}</span>
                                    <span className="block text-[11px] leading-tight text-muted">{p.hint}</span>
                                </span>
                            </button>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
