import React, { useState, useEffect, useRef, useMemo } from 'react';
import { Activity, Plus, History, X, Terminal, ChevronDown, RotateCcw, Loader2, Download, Globe, Bot } from 'lucide-react';
import MediaInput from './components/MediaInput';
import ResultCard from './components/ResultCard';
import ProcessingAnimation from './components/ProcessingAnimation';
import ClipEditor from './components/ClipEditor';
import ReframeEditor from './components/ReframeEditor';
import Modal from './components/ui/Modal';
import { useAuth } from './contexts/AuthContext';
import { apiFetch } from './lib/api';
import { JobPollError, pollFailureAction } from './lib/jobPoll';

const SESSION_KEY = 'openshorts_session';
// Matches the JOB_RETENTION_SECONDS default: a restore whose job was already
// purged server-side gets a 404 from the status poll, which clears the saved
// session (see lib/jobPoll.js).
const SESSION_MAX_AGE = 86400000; // 24 hours

const formatRetention = (seconds) => {
  if (seconds >= 86400) return `${Math.round(seconds / 86400)} дн.`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} ч`;
  return `${Math.max(1, Math.round(seconds / 60))} мин`;
};

// The server's own explanation for a rejected/failed job, readable: FastAPI
// answers {"detail": "..."} or {"detail": {"message": ...}}.
const readableError = (raw) => {
  const text = String(raw || '').trim();
  try {
    const body = JSON.parse(text);
    const d = body?.detail ?? body;
    if (typeof d === 'string' && d) return d.slice(0, 300);
    if (d && typeof d.message === 'string') return d.message.slice(0, 300);
  } catch (_) { /* not JSON */ }
  return text.replace(/^Error:\s*/, '').slice(0, 300) || 'Что-то пошло не так.';
};

const pollJob = async (jobId) => {
  const res = await apiFetch(`/api/status/${jobId}`);
  if (!res.ok) throw new JobPollError(res.status);
  return res.json();
};

function App() {
  const { jobRetentionSeconds, agentClips } = useAuth();
  // Why the last job could not start or failed, in plain words (or '').
  const [jobError, setJobError] = useState('');
  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState('idle'); // idle, processing, complete, error
  const [results, setResults] = useState(null);
  // {position, ahead, eta_seconds} while the job waits in line, else null.
  const [queueInfo, setQueueInfo] = useState(null);
  // Best clips first. The ORIGINAL array position travels with each clip and is
  // passed down as `index`: it is the clip's identity everywhere else
  // (clip_index on /api/subtitle and /api/hook, the clip-N.mp4 download name).
  const rankedClips = useMemo(() => {
    const clips = results?.clips;
    if (!Array.isArray(clips)) return [];
    return clips
      .map((clip, index) => ({ clip, index }))
      .sort((a, b) => {
        const sa = Number.isFinite(a.clip?.predicted_score) ? a.clip.predicted_score : -1;
        const sb = Number.isFinite(b.clip?.predicted_score) ? b.clip.predicted_score : -1;
        return sb - sa || a.index - b.index;
      });
  }, [results]);
  // Bulk subtitles: apply one style to every clip of the job.
  const [bulkSub, setBulkSub] = useState({ running: false, current: 0, total: 0, errors: 0 });
  const [downloadingAll, setDownloadingAll] = useState(false);
  // Pre-flight quality gate: { info: {max_height, min_height, cookies_invalid}, data }
  const [qualityGate, setQualityGate] = useState(null);
  const [logs, setLogs] = useState([]);
  // When each server log line arrived (epoch s, parallel to logs), from /api/status.
  const [logTimes, setLogTimes] = useState([]);
  // Collapsed on phones: the log tail is the least useful thing on a 360px screen.
  const [logsVisible, setLogsVisible] = useState(() => {
    try { return window.innerWidth >= 768; } catch { return true; }
  });
  const [processingMedia, setProcessingMedia] = useState(null);
  const [sessionRecovered, setSessionRecovered] = useState(false);
  // The job a restored session was polling is no longer on the server.
  const [jobGone, setJobGone] = useState(false);
  // Clip editor overlay: index of the clip being edited, or null.
  const [editingClip, setEditingClip] = useState(null);
  const [reframingClip, setReframingClip] = useState(null);

  // Sync state for original video playback
  const [syncedTime, setSyncedTime] = useState(0);
  const [isSyncedPlaying, setIsSyncedPlaying] = useState(false);
  const [syncTrigger, setSyncTrigger] = useState(0);

  const handleClipPlay = (startTime) => {
    setSyncedTime(startTime);
    setIsSyncedPlaying(true);
    setSyncTrigger(prev => prev + 1);
  };

  const handleClipPause = () => {
    setIsSyncedPlaying(false);
  };

  // A recut replaced the clip's server file with a fresh render (burned layers
  // reset), so update the results and let the ResultCard remount from the new file.
  const handleClipRerendered = (index, data) => {
    setResults((prev) => {
      if (!prev?.clips?.[index]) return prev;
      const clips = prev.clips.slice();
      clips[index] = {
        ...clips[index],
        video_url: data.new_video_url,
        start: data.start,
        end: data.end,
        recipe: data.recipe,
      };
      return { ...prev, clips };
    });
  };

  // Apply one subtitle style to every clip of the job, sequentially.
  const handleBulkSubtitles = async (options) => {
    const clips = results?.clips || [];
    const total = clips.length;
    if (!total) return;
    setBulkSub({ running: true, current: 0, total, errors: 0 });
    let errors = 0;
    for (let i = 0; i < total; i++) {
      setBulkSub({ running: true, current: i + 1, total, errors });
      try {
        const res = await apiFetch('/api/subtitle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: jobId,
            clip_index: i,
            position: options.position,
            font_size: options.fontSize,
            font_name: options.fontName,
            font_color: options.fontColor,
            border_color: options.borderColor,
            border_width: options.borderWidth,
            bg_color: options.bgColor,
            bg_opacity: options.bgOpacity,
            style: options.style || 'pill',
            highlight_color: options.highlightColor || '#FFD700',
            effect: options.effect || 'none',
            base_opacity: options.baseOpacity ?? 1.0,
            uppercase: options.uppercase || false,
            reveal: options.reveal || false,
            shadow: options.shadow || 0,
            max_chars: options.maxChars ?? null,
            max_duration: options.maxDuration ?? null,
            // Chain from the clip's current server file (its video_url basename).
            input_filename: (clips[i].video_url || '').split('/').pop(),
          }),
        });
        if (!res.ok) errors++;
      } catch {
        errors++;
      }
    }
    setBulkSub({ running: false, current: total, total, errors });
    // Refresh results so each ResultCard picks up its new subtitled video_url.
    try {
      const data = await pollJob(jobId);
      if (data.result) setResults(data.result);
    } catch { /* keep current results */ }
  };

  const handleDownloadAll = async () => {
    if (!jobId) return;
    setDownloadingAll(true);
    try {
      const res = await apiFetch(`/api/jobs/${jobId}/download-all`);
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `nares_clips_${(jobId || '').slice(0, 8)}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(`Не удалось скачать: ${e.message}`);
    } finally {
      setDownloadingAll(false);
    }
  };

  // Session Recovery: Restore on mount
  useEffect(() => {
    try {
      const saved = localStorage.getItem(SESSION_KEY);
      if (!saved) return;
      const session = JSON.parse(saved);
      if (Date.now() - session.timestamp > SESSION_MAX_AGE) {
        localStorage.removeItem(SESSION_KEY);
        return;
      }
      if (session.jobId && session.status && session.status !== 'idle') {
        setJobId(session.jobId);
        setResults(session.results || null);
        // Restore the source preview: saved media, else the backend-served source.
        if (session.processingMedia) setProcessingMedia(session.processingMedia);
        else setProcessingMedia({ type: 'server', payload: `/api/source/${session.jobId}` });
        // If was processing, resume polling; if complete/error, just show results
        setStatus(session.status === 'processing' ? 'processing' : session.status);
        setSessionRecovered(true);
        setTimeout(() => setSessionRecovered(false), 5000);
      }
    } catch (e) {
      localStorage.removeItem(SESSION_KEY);
    }
  }, []);

  // Session Recovery: Save state changes
  useEffect(() => {
    if (status === 'idle') {
      localStorage.removeItem(SESSION_KEY);
      return;
    }
    try {
      // URL media serializes as-is. Uploaded 'file' media is a blob that can't be
      // persisted, so point the recovered preview at the backend-served source.
      let persistMedia = null;
      if (processingMedia?.type === 'url') persistMedia = processingMedia;
      else if (processingMedia && jobId) persistMedia = { type: 'server', payload: `/api/source/${jobId}` };
      localStorage.setItem(SESSION_KEY, JSON.stringify({
        jobId,
        status,
        results,
        processingMedia: persistMedia,
        timestamp: Date.now(),
      }));
    } catch (e) {
      // localStorage full or serialization error - ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, status, results]);

  const handleResetRef = useRef(null);

  useEffect(() => {
    let interval;
    let failures = 0;
    if ((status === 'processing' || status === 'completed') && jobId) {
      interval = setInterval(async () => {
        try {
          const data = await pollJob(jobId);
          failures = 0;

          // Update results if available (real-time)
          if (data.result) {
            setResults(data.result);
          }

          setQueueInfo(data.status === 'queued' && data.queue ? data.queue : null);

          if (data.status === 'completed') {
            setQueueInfo(null);
            setStatus('complete');
            clearInterval(interval);
          } else if (data.status === 'failed') {
            setStatus('error');
            const errorMsg = data.error || (data.logs && data.logs.length > 0 ? data.logs[data.logs.length - 1] : 'Обработка не удалась');
            setJobError(readableError(errorMsg));
            setLogs(prev => [...prev, 'Ошибка: ' + errorMsg]);
            clearInterval(interval);
          } else if (data.logs) {
            setLogs(data.logs);
            setLogTimes(data.log_times || []);
          }
        } catch (e) {
          console.error('Polling error', e);
          failures += 1;
          if (pollFailureAction(e, failures) === 'gone') {
            clearInterval(interval);
            handleResetRef.current();
            setSessionRecovered(false);
            setJobGone(true);
          }
        }
      }, 2000);
    }
    return () => clearInterval(interval);
  }, [status, jobId]);

  const handleProcess = async (data, forceLowQuality = false) => {
    setStatus('processing');
    setJobError('');
    setLogs(['Запускаем…']);
    setLogTimes([Date.now() / 1000]);
    setResults(null);
    setProcessingMedia(data);
    setQualityGate(null);

    try {
      let body;
      const headers = {};

      // Advanced generation controls: only sent when the user set them, so the
      // default request stays byte-identical to the pre-feature one.
      const advanced = {
        target_clips: data.targetClips || null,
        clip_min_seconds: data.clipMinSeconds || null,
        clip_max_seconds: data.clipMaxSeconds || null,
        // Sent explicitly both ways: absent means off for raw API callers,
        // but the dashboard always states the user's choice.
        auto_hook: data.autoHook ? '1' : '0',
        auto_hook_style: data.autoHook ? (data.autoHookStyle || 'pill') : null,
        // 'auto' is the server default, so only a deliberate choice travels.
        layouts: data.layout && data.layout !== 'auto' ? data.layout : null,
        project_id: data.projectId || null,
        banner_id: data.bannerId || null,
        banner_position: data.bannerId ? data.bannerPlace : null,
      };

      if (data.type === 'url') {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify({
          url: data.payload,
          acknowledged: !!data.acknowledged,
          output_format: data.outputFormat || 'auto',
          force_low_quality: forceLowQuality,
          ...Object.fromEntries(Object.entries(advanced).filter(([, v]) => v != null)),
        });
      } else {
        const formData = new FormData();
        formData.append('file', data.payload);
        formData.append('acknowledged', data.acknowledged ? 'true' : 'false');
        formData.append('output_format', data.outputFormat || 'auto');
        for (const [k, v] of Object.entries(advanced)) {
          if (v != null) formData.append(k, v);
        }
        body = formData;
      }

      const res = await apiFetch('/api/process', { method: 'POST', headers, body });

      if (!res.ok) throw new Error(await res.text());
      const resData = await res.json();

      // Quality gate: the source is below the min resolution: ask first.
      // On confirm we resend with force_low_quality.
      if (resData.needs_confirmation) {
        setStatus('idle');
        setQualityGate({ info: resData.quality_check, data });
        return;
      }

      setJobId(resData.job_id);
    } catch (e) {
      const reason = readableError(e.message);
      setJobError(reason);
      setStatus('error');
      setLogs(l => [...l, `Не удалось запустить: ${reason}`]);
    }
  };

  const handleReset = () => {
    setJobGone(false);
    setStatus('idle');
    setJobId(null);
    setResults(null);
    setLogs([]);
    setLogTimes([]);
    setProcessingMedia(null);
    setQueueInfo(null);
    setJobError('');
    localStorage.removeItem(SESSION_KEY);
  };
  useEffect(() => {
    handleResetRef.current = handleReset;
  });

  const statusLabel = { processing: 'В РАБОТЕ', complete: 'ГОТОВО', error: 'ОШИБКА' }[status] || '';

  return (
    /* h-dvh where supported: on mobile `100vh` is the tallest the viewport ever
       gets, so a h-screen shell hides its own bottom behind the browser chrome. */
    <div className="flex flex-col h-screen supports-[height:100dvh]:h-[100dvh] bg-paper overflow-hidden">
      <header className="h-14 border-b border-rule bg-paper flex items-center justify-between gap-2 px-3 sm:px-6 shrink-0 z-10">
        <a href="#landing" className="font-display lowercase text-lg text-ink" title="На главную">нарез</a>
        <div className="flex items-center gap-2 sm:gap-3">
          {status !== 'idle' && (
            <button
              onClick={handleReset}
              className="btn-quiet px-3 py-1.5 text-xs shrink-0"
              aria-label="Новый проект"
            >
              <Plus size={14} />
              <span className="hidden sm:inline">Новый проект</span>
            </button>
          )}
          <a href="#landing" className="flex items-center gap-1.5 text-xs lowercase text-muted hover:text-ink2 transition-colors">
            <Globe size={14} className="shrink-0" />
            <span className="hidden sm:inline">о сервисе</span>
          </a>
        </div>
      </header>

      {/* Session Recovery Banner */}
      {sessionRecovered && (
        <div className="mx-3 sm:mx-6 mt-2 px-3.5 sm:px-4 py-3 bg-paper2 border border-rule rounded-card flex items-start justify-between gap-3 animate-fade shrink-0">
          <div className="flex items-start sm:items-center gap-2 text-sm text-ink2 flex-wrap min-w-0">
            <RotateCcw size={16} className="text-brass shrink-0 mt-0.5 sm:mt-0" />
            <span className="font-medium">Сессия восстановлена</span>
            <span className="text-muted text-xs">Твоя предыдущая работа на месте.</span>
          </div>
          <button
            onClick={() => setSessionRecovered(false)}
            aria-label="закрыть"
            className="text-muted hover:text-ink transition-colors shrink-0 -m-1 p-1"
          >
            <X size={16} />
          </button>
        </div>
      )}

      {/* The restored job is no longer on the server (purged). */}
      {jobGone && (
        <div role="status" className="mx-3 sm:mx-6 mt-2 px-3.5 sm:px-4 py-3 bg-paper2 border border-rule rounded-card flex items-start justify-between gap-3 animate-fade shrink-0">
          <div className="flex items-start sm:items-center gap-2 text-sm text-ink2 flex-wrap min-w-0">
            <History size={16} className="text-brass shrink-0 mt-0.5 sm:mt-0" />
            <span className="font-medium">Этот проект уже закрыт</span>
            <span className="text-muted text-xs">Сервер его очистил. Начни новый проект.</span>
          </div>
          <button
            onClick={() => setJobGone(false)}
            aria-label="закрыть"
            className="text-muted hover:text-ink transition-colors shrink-0 -m-1 p-1"
          >
            <X size={16} />
          </button>
        </div>
      )}

      {/* Main Workspace */}
      <main className="flex-1 overflow-hidden relative">

        {/* Idle */}
        {status === 'idle' && (
          <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
            <div className="min-h-full flex flex-col items-center justify-center px-4 py-5 sm:p-6">
              <div className="max-w-xl w-full text-center space-y-5 sm:space-y-8">
                <div className="space-y-2.5 sm:space-y-4">
                  <h1 className="font-display lowercase text-3xl sm:text-4xl md:text-5xl text-ink">
                    Нарезка клипов
                  </h1>
                  <p className="text-muted text-[15px] sm:text-lg leading-snug sm:leading-normal max-w-sm sm:max-w-none mx-auto">
                    Вставь ссылку или загрузи файл, а дальше НАРЕЗ нарежет вертикальные клипы.
                  </p>
                  {agentClips && (
                    <p className="text-xs text-muted flex items-center justify-center gap-1.5">
                      <Bot size={14} className="text-brass shrink-0" />
                      Моменты для клипов выбирает твой агент, ключи не нужны.
                    </p>
                  )}
                </div>

                <MediaInput onProcess={handleProcess} isProcessing={status === 'processing'} />
              </div>
            </div>
          </div>
        )}

        {/* Processing / Results (Split View) */}
        {(status === 'processing' || status === 'complete' || status === 'error') && (
          <div className="h-full flex flex-col md:flex-row gap-3 md:gap-4 p-3 md:p-4 overflow-y-auto md:overflow-y-hidden custom-scrollbar animate-fade">

            {/* Left Panel: Preview & Status */}
            <div className={`${status === 'complete' ? 'w-full md:w-[30%] lg:w-[25%]' : 'w-full md:w-[55%] lg:w-[60%]'} md:h-full flex flex-col shrink-0 md:shrink card p-3.5 sm:p-6 md:overflow-y-auto custom-scrollbar transition-all duration-700 ease-in-out`}>
              <div className="mb-4 sm:mb-6 flex items-center justify-between gap-2">
                <h2 className="text-sm font-medium text-ink lowercase flex items-center gap-2">
                  <Activity className={`text-brass ${status === 'processing' ? 'animate-pulse' : ''}`} size={18} />
                  Ход работы
                </h2>
                <span className={status === 'processing' ? 'badge-brass' :
                  status === 'complete' ? 'badge-ok' :
                    'badge-danger'
                  }>
                  {statusLabel}
                </span>
              </div>

              {/* Waiting in line: say where and for how long. */}
              {status === 'processing' && queueInfo && (
                <div className="mb-4 rounded-card border border-brass/40 bg-brass/5 px-4 py-3 text-sm">
                  <p className="text-ink">
                    {queueInfo.ahead === 0
                      ? 'Ты следующий в очереди. Начинаем…'
                      : <>Ты <b>#{queueInfo.position}</b> в очереди · {queueInfo.eta_is_minimum ? 'не меньше' : 'примерно'} <b>{Math.max(1, Math.round(queueInfo.eta_seconds / 60))} мин</b></>}
                  </p>
                </div>
              )}

              {/* Agent mode: the job waits for the agent's picks. */}
              {status === 'processing' && agentClips && (
                <div className="mb-4 rounded-card border border-rule bg-paper2 px-4 py-3 text-sm text-ink2 flex items-start gap-2.5">
                  <Bot size={16} className="text-brass shrink-0 mt-0.5" />
                  <span>Клипы выбирает твой агент. Как только он пришлёт моменты, они появятся справа.</span>
                </div>
              )}

              {/* Video Preview */}
              {processingMedia && (
                <ProcessingAnimation
                  media={processingMedia}
                  isComplete={status === 'complete'}
                  syncedTime={syncedTime}
                  isSyncedPlaying={isSyncedPlaying}
                  syncTrigger={syncTrigger}
                />
              )}

              {/* Phones only: the scan box drops its telemetry at this size and
                  the log terminal starts collapsed, so show the log tail. */}
              {status === 'processing' && (
                <div className="sm:hidden mb-3 flex items-start gap-2 text-xs text-ink2 min-w-0">
                  <Loader2 size={14} className="animate-spin text-brass shrink-0 mt-px" />
                  <span className="min-w-0 leading-snug break-words">
                    {logs.length ? logs[logs.length - 1] : 'запускаемся…'}
                  </span>
                </div>
              )}

              {/* Logs Terminal */}
              <div className={`bg-paper rounded-card border border-rule overflow-hidden flex flex-col transition-all duration-500 ${status === 'complete' ? `min-h-0 opacity-50 hover:opacity-100 ${logsVisible ? 'h-32' : 'h-auto'}` : `flex-1 ${logsVisible ? 'min-h-[160px] sm:min-h-[200px]' : 'min-h-0 flex-none'}`}`}>
                <button
                  type="button"
                  onClick={() => setLogsVisible(!logsVisible)}
                  aria-expanded={logsVisible}
                  className="w-full px-3.5 sm:px-4 py-2.5 border-b border-rule flex items-center justify-between gap-2 bg-paper2 shrink-0 text-left"
                >
                  <span className="readout flex items-center gap-2">
                    <Terminal size={12} /> Журнал
                  </span>
                  <span className="flex items-center gap-2 text-muted">
                    {!logsVisible && logs.length > 0 && (
                      <span className="readout normal-case">{logs.length}</span>
                    )}
                    <ChevronDown size={16} className={logsVisible ? '' : 'rotate-180'} />
                  </span>
                </button>
                {logsVisible && (
                  <div className="flex-1 p-3.5 sm:p-4 overflow-y-auto font-mono text-[11px] sm:text-xs space-y-1.5 custom-scrollbar text-muted break-words">
                    {logs.map((log, i) => (
                      <div key={i} className={`flex gap-2 ${/error|ошибка/i.test(log) ? 'text-danger' : 'text-muted'}`}>
                        <span className="text-muted opacity-50 shrink-0 hidden sm:inline tabular-nums">
                          {logTimes[i] ? new Date(logTimes[i] * 1000).toLocaleTimeString() : ''}
                        </span>
                        <span className="min-w-0 break-words">{log}</span>
                      </div>
                    ))}
                    {status === 'processing' && (
                      <div className="animate-pulse text-brass">_</div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Right Panel: Results Grid */}
            <div className={`${status === 'complete' ? 'w-full md:w-[70%] lg:w-[75%]' : 'w-full md:w-[45%] lg:w-[40%]'} md:h-full flex flex-col shrink-0 md:shrink card p-3.5 sm:p-6 transition-all duration-700 ease-in-out`}>
              <div className="mb-4 sm:mb-6 shrink-0 space-y-3">
                <h2 className="font-display lowercase text-lg sm:text-xl text-ink flex flex-wrap items-center gap-2">
                  <span className="mr-auto">Готовые клипы</span>
                  {results?.clips?.length > 0 && (
                    <span className="readout bg-paper3 px-2.5 py-1 rounded-full">
                      {results.clips.length} шт.
                    </span>
                  )}
                </h2>
                {results?.clips?.length > 0 && status === 'complete' && (
                  <div className="flex flex-col sm:flex-row sm:justify-end items-stretch sm:items-center gap-2">
                    <button
                      onClick={handleDownloadAll}
                      disabled={downloadingAll}
                      className="btn-ghost px-3 py-2 text-xs"
                      title="Скачать все клипы одним ZIP"
                    >
                      {downloadingAll
                        ? <><Loader2 size={14} className="animate-spin" />собираем архив…</>
                        : <><Download size={14} />скачать всё</>}
                    </button>
                  </div>
                )}
              </div>

              {status === 'complete' && results?.clips?.length > 0 && jobRetentionSeconds > 0 && (
                <div className="mb-2 px-3 py-2.5 rounded-input bg-paper3 border border-paper3 text-sm">
                  <span className="text-ink">Клипы хранятся {formatRetention(jobRetentionSeconds)}, потом удаляются.</span>{' '}
                  <span className="text-muted">Скачай всё нужное или увеличь JOB_RETENTION_SECONDS в настройках сервера.</span>
                </div>
              )}

              <div className="flex-1 overflow-y-auto custom-scrollbar p-1">
                {results && results.clips && results.clips.length > 0 ? (
                  <div className={`grid gap-4 pb-10 ${status === 'complete' ? 'grid-cols-[repeat(auto-fill,minmax(min(100%,600px),1fr))]' : 'grid-cols-1'}`}>
                    {rankedClips.map(({ clip, index: i }) => (
                      <ResultCard
                        key={`${jobId}-${i}-${clip.video_url || ''}`}
                        clip={clip}
                        index={i}
                        jobId={jobId}
                        onEditClip={(index) => setEditingClip(index)}
                        onReframeClip={(index) => setReframingClip(index)}
                        onPlay={(time) => handleClipPlay(time)}
                        onPause={handleClipPause}
                        onBulkSubtitle={handleBulkSubtitles}
                        clipCount={results.clips.length}
                        bulkProgress={bulkSub}
                      />
                    ))}
                  </div>
                ) : (
                  status === 'processing' ? (
                    <div className="h-full min-h-[140px] flex flex-col items-center justify-center text-muted space-y-3 text-center px-4">
                      <Loader2 size={28} className="animate-spin text-brass" />
                      <p className="text-sm lowercase">Ждём клипы…</p>
                      <p className="text-xs text-muted/80 max-w-[26ch] leading-snug">
                        Они появляются здесь по одному, как только отрендерятся.
                      </p>
                    </div>
                  ) : status === 'error' ? (
                    <div className="h-full min-h-[120px] flex flex-col items-center justify-center text-danger space-y-2 text-center px-4">
                      <p>Не получилось.</p>
                      {jobError && <p className="text-xs break-words">{jobError}</p>}
                    </div>
                  ) : null
                )}
              </div>
            </div>

          </div>
        )}

      </main>

      {/* Pre-flight quality gate */}
      {qualityGate && (
        <Modal isOpen={true} onClose={() => setQualityGate(null)} size="md" eyebrow="ВНИМАНИЕ" title="низкое качество источника">
          <div className="space-y-4">
            <p className="text-sm text-ink2">
              YouTube отдаёт для этого видео только <span className="text-brass font-semibold">{qualityGate.info.max_height}p</span>{' '}
              (ниже рекомендуемых {qualityGate.info.min_height}p). Если продолжить, клипы выйдут в худшем качестве.
            </p>
            {qualityGate.info.cookies_invalid && (
              <p className="text-xs text-muted">
                Похоже, cookies YouTube протухли. Обнови их (экспортируй заново из режима инкогнито), и часто открывается HD.
              </p>
            )}
            <div className="flex gap-2 justify-end pt-2">
              <button onClick={() => setQualityGate(null)} className="btn-ghost">отмена</button>
              <button
                onClick={() => { const d = qualityGate.data; setQualityGate(null); handleProcess(d, true); }}
                className="btn-primary"
              >
                всё равно нарезать
              </button>
            </div>
          </div>
        </Modal>
      )}

      {editingClip !== null && results?.clips?.[editingClip] && (
        <ClipEditor
          jobId={jobId}
          clipIndex={editingClip}
          clipTitle={results.clips[editingClip].video_title_for_youtube_short || ''}
          onClose={() => setEditingClip(null)}
          onRerendered={handleClipRerendered}
        />
      )}
      {reframingClip !== null && results?.clips?.[reframingClip] && (
        <ReframeEditor
          jobId={jobId}
          clipIndex={reframingClip}
          clipTitle={results.clips[reframingClip].video_title_for_youtube_short || ''}
          onClose={() => setReframingClip(null)}
          onReframed={handleClipRerendered}
        />
      )}
    </div>
  );
}

export default App;
