import React, { useState, useRef, useEffect } from 'react';
import { X, ImagePlus, Loader2 } from 'lucide-react';
import { apiFetch } from '../lib/api';

// Frame editor of a project: two boxes (game, webcam) over a preview frame,
// stored normalised 0..1. Nothing is uploaded: a local image stays in the browser.
const MIN = 0.05;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const COLORS = { game: '#f97316', cam: '#22d3ee' };
const LABELS = { game: 'Игра', cam: 'Вебка' };
const CORNERS = { nw: [1, 1, 0, 0], ne: [0, 1, 1, 0], sw: [1, 0, 0, 1], se: [0, 0, 1, 1] }; // moves left, top, right, bottom

function youtubeId(url) {
    const m = /(?:youtu\.be\/|[?&]v=|\/shorts\/|\/embed\/)([\w-]{11})/.exec(url || '');
    return m ? m[1] : '';
}

export default function FrameBoxEditor({ project, url, banner, onSaved, onClose }) {
    const [name, setName] = useState(project?.name || '');
    const [boxes, setBoxes] = useState({
        game: project?.game || { x: 0, y: 0, w: 0.7, h: 1 },
        cam: project?.cam || { x: 0.72, y: 0.55, w: 0.26, h: 0.4 },
    });
    const ytId = youtubeId(url);
    const [local, setLocal] = useState('');
    const [thumb, setThumb] = useState(ytId ? 'maxresdefault' : '');
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const frameRef = useRef(null);
    const dragRef = useRef(null);
    const fileRef = useRef(null);
    const src = local || (ytId && thumb ? `https://img.youtube.com/vi/${ytId}/${thumb}.jpg` : '');

    useEffect(() => () => { if (local) URL.revokeObjectURL(local); }, [local]);

    const onImgLoad = (e) => { // YouTube answers a 120px grey placeholder when maxres is missing
        if (!local && thumb === 'maxresdefault' && e.target.naturalWidth <= 120) setThumb('hqdefault');
    };

    const start = (key, corner) => (e) => {
        e.preventDefault();
        e.stopPropagation();
        e.currentTarget.setPointerCapture(e.pointerId);
        dragRef.current = { key, corner, px: e.clientX, py: e.clientY, box: boxes[key] };
    };

    const move = (e) => {
        const d = dragRef.current;
        if (!d) return;
        const r = frameRef.current.getBoundingClientRect();
        const dx = (e.clientX - d.px) / r.width;
        const dy = (e.clientY - d.py) / r.height;
        const { x, y, w, h } = d.box;
        let next;
        if (!d.corner) {
            next = { x: clamp(x + dx, 0, 1 - w), y: clamp(y + dy, 0, 1 - h), w, h };
        } else {
            const [l, t, rt, b] = CORNERS[d.corner];
            const x0 = clamp(x + l * dx, 0, x + w - MIN);
            const y0 = clamp(y + t * dy, 0, y + h - MIN);
            const x1 = clamp(x + w + rt * dx, x0 + MIN, 1);
            const y1 = clamp(y + h + b * dy, y0 + MIN, 1);
            next = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
        }
        setBoxes((s) => ({ ...s, [d.key]: next }));
    };

    const save = async () => {
        if (!name.trim()) { setError('введите название'); return; }
        setError('');
        setBusy(true);
        try {
            const body = {
                name: name.trim(), game: boxes.game, cam: boxes.cam,
                banner_id: banner?.id || null, banner_position: banner?.id ? banner.place : null,
            };
            const res = await apiFetch(project ? `/api/projects/${project.id}` : '/api/projects', {
                method: project ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'не удалось сохранить');
            onSaved(await res.json());
        } catch (e) {
            setError(e.message);
        } finally {
            setBusy(false);
        }
    };

    // Stretch of a box over a 9:16 slot: background cropped to the box.
    const crop = (b) => src ? {
        backgroundImage: `url(${src})`,
        backgroundSize: `${100 / b.w}% ${100 / b.h}%`,
        backgroundPosition: `${b.w < 1 ? (b.x / (1 - b.w)) * 100 : 0}% ${b.h < 1 ? (b.y / (1 - b.h)) * 100 : 0}%`,
    } : {};

    return (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-3" role="dialog" aria-modal="true">
            <div className="card p-4 sm:p-6 w-full max-w-3xl max-h-full overflow-y-auto">
                <div className="flex items-center justify-between mb-3">
                    <p className="eyebrow">{project ? 'Изменить проект' : 'Новый проект'}</p>
                    <button type="button" onClick={onClose} aria-label="закрыть" className="p-1 text-muted hover:text-ink"><X size={16} /></button>
                </div>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Название проекта" className="input-field mb-3" />
                <div className="flex flex-col sm:flex-row gap-4">
                    <div className="flex-1 min-w-0">
                        <div ref={frameRef} className="relative aspect-video rounded-input border border-rule2 overflow-hidden bg-black/40 select-none touch-none">
                            {src && <img src={src} alt="" onLoad={onImgLoad} onError={() => { if (!local && thumb === 'maxresdefault') setThumb('hqdefault'); else if (!local) setThumb(''); }}
                                         className="absolute inset-0 w-full h-full object-fill pointer-events-none" draggable={false} />}
                            {!src && <span className="absolute inset-0 flex items-center justify-center text-xs text-muted">загрузите кадр из стрима</span>}
                            {Object.entries(boxes).map(([key, b]) => (
                                <div key={key} onPointerDown={start(key)} onPointerMove={move} onPointerUp={() => { dragRef.current = null; }}
                                     className="absolute cursor-move touch-none"
                                     style={{ left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%`,
                                              border: `2px solid ${COLORS[key]}`, background: `${COLORS[key]}22`, zIndex: key === 'cam' ? 2 : 1 }}>
                                    <span className="absolute top-0 left-0 px-1 text-[10px] text-black" style={{ background: COLORS[key] }}>{LABELS[key]}</span>
                                    {Object.keys(CORNERS).map((c) => (
                                        <span key={c} onPointerDown={start(key, c)} onPointerMove={move} onPointerUp={() => { dragRef.current = null; }}
                                              className="absolute w-4 h-4 touch-none"
                                              style={{ background: COLORS[key], cursor: `${c}-resize`,
                                                       [c[0] === 'n' ? 'top' : 'bottom']: -6, [c[1] === 'w' ? 'left' : 'right']: -6 }} />
                                    ))}
                                </div>
                            ))}
                        </div>
                        <button type="button" onClick={() => fileRef.current?.click()}
                                className="mt-2 flex items-center gap-1.5 text-xs text-muted hover:text-ink2">
                            <ImagePlus size={14} /> {src ? 'другой кадр' : 'выбрать кадр'} (картинка с компьютера)
                        </button>
                        <input ref={fileRef} type="file" accept="image/*" className="hidden"
                               onChange={(e) => { const f = e.target.files[0]; if (f) setLocal(URL.createObjectURL(f)); e.target.value = ''; }} />
                    </div>
                    <div className="shrink-0 mx-auto">
                        <p className="text-[11px] text-muted mb-1 text-center">Результат 9:16</p>
                        <div className="w-[108px] h-[192px] rounded-input border border-rule2 overflow-hidden bg-black/40 flex flex-col">
                            <div style={{ height: '60%', ...crop(boxes.game) }} />
                            <div style={{ height: '40%', ...crop(boxes.cam) }} />
                        </div>
                    </div>
                </div>
                {error && <p className="mt-2 text-xs text-warn">{error}</p>}
                <button type="button" onClick={save} disabled={busy} className="w-full btn-primary mt-4">
                    {busy ? <Loader2 size={16} className="animate-spin" /> : 'Сохранить проект'}
                </button>
            </div>
        </div>
    );
}
