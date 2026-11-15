// Главная страница НАРЕЗ: продуктовая страница для тех, кто уже получил репозиторий.
// Дизайн перенесён из промо-страницы владельца; стили в landing.css (область .nz).
import React, { useEffect, useRef, useState } from 'react';
import SiteNav from './components/SiteNav';
import DEFS from './landingDefs.js';
import './landing.css';

const reduceMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
const scrollTo = (id) => (e) => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' }); };

const Icon = ({ id }) => <svg viewBox="0 0 24 24" aria-hidden="true"><use href={`#${id}`} /></svg>;

const BANNERS = {
  tg: { icon: 'i-plane', text: '@твой_канал' },
  ad: { icon: 'i-shield', text: 'Твоя реклама' },
  own: { icon: 'i-star', text: 'ТВОЙ БРЕНД' },
};

// Телефон с вертикальным клипом: сцена, баннер, хук и субтитры, где подсвечивается одно слово.
function Phone({ scene, banner = 'tg', bot = false, hook, words, tick, id }) {
  const b = BANNERS[banner];
  return (
    <div className="phone" data-s={scene} id={id}>
      <svg viewBox="0 0 180 320" aria-hidden="true"><use href={`#sc-${scene === 'game' ? 'gta' : scene}`} /></svg>
      <div className={`bnr ${banner}${bot ? ' bot' : ''}`}><Icon id={b.icon} /><span>{b.text}</span></div>
      <div className="hook">{hook}</div>
      <div className="subs">
        {words.map((w, i) => (
          <React.Fragment key={i}>{i > 0 && ' '}<span className={i === tick % words.length ? 'on' : undefined}>{w}</span></React.Fragment>
        ))}
      </div>
    </div>
  );
}

const NICHES = [
  { scene: 'game', banner: 'tg', hook: 'Такого ещё не было', words: ['копы', 'отстали'], title: 'Игры', note: 'Моменты из прохождений' },
  { scene: 'film', banner: 'ad', hook: 'Концовку никто не понял', words: ['он', 'знал', 'всё'], title: 'Фильмы и сериалы', note: 'Разборы и сцены' },
  { scene: 'stream', banner: 'own', hook: 'Чат не поверил', words: ['чат', 'в', 'шоке'], title: 'Стримы', note: 'Игра сверху, вебка снизу' },
  { scene: 'pod', banner: 'tg', bot: true, hook: 'Он сказал это в эфире', words: ['это', 'работает'], title: 'Подкасты', note: 'Два спикера в кадре' },
];

const STEPS = [
  ['Ссылка или файл', 'YouTube, Twitch или своё видео'],
  ['Скачивание', 'видео в лучшем качестве'],
  ['Получаем текст', 'речь → слова с таймкодами'],
  ['Агент выбирает', 'находит сильные моменты'],
  ['Нарезка на сцены', 'клипы 15–60 с, кроп 9:16'],
  ['Сборка', 'субтитры, хук и баннер'],
];
const AT = [0, 900, 2000, 3000, 4200, 5200, 6300];

function Reel() {
  const [n, setN] = useState(0);
  const timers = useRef([]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const run = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    setN(0);
    if (reduceMotion()) return setN(7);
    AT.forEach((t, i) => timers.current.push(setTimeout(() => setN(i + 1), t)));
  };
  const cls = ['screen', ...[1, 2, 3, 4, 5, 6].filter((j) => j <= n).map((j) => `s${j}`), n > 6 ? 'end' : ''].join(' ');
  return (
    <div className="panel reel">
      <div className="reel-h">
        <h2>Как это работает</h2>
        <p>Нажми Play на видео — пройдём весь путь от ссылки до готовых клипов.</p>
      </div>
      <div className={cls}>
        <svg viewBox="0 0 640 360" aria-hidden="true" preserveAspectRatio="xMidYMid slice">
          <rect width="640" height="360" fill="url(#cityG)" />
          <circle cx="470" cy="120" r="56" fill="#ff3d5a" />
          <g fill="#140a30">
            <rect x="0" y="180" width="50" height="180" /><rect x="46" y="140" width="40" height="220" /><rect x="84" y="200" width="60" height="160" /><rect x="140" y="110" width="34" height="250" /><rect x="172" y="170" width="54" height="190" />
            <rect x="226" y="130" width="40" height="230" /><rect x="264" y="190" width="70" height="170" /><rect x="330" y="150" width="44" height="210" /><rect x="372" y="200" width="58" height="160" /><rect x="430" y="160" width="40" height="200" />
            <rect x="468" y="210" width="64" height="150" /><rect x="530" y="140" width="44" height="220" /><rect x="572" y="190" width="68" height="170" />
          </g>
          <path d="M0 300c80-20 160 10 240-6s200-30 400 2v64H0z" fill="#0b0618" />
        </svg>
        <div className="badge" aria-live="polite">{n >= 1 && n <= 6 ? `${n}/6 · ${STEPS[n - 1][0]}` : 'Готово: 3 клипа'}</div>
        <div className="url" aria-hidden="true">youtube.com/watch?v=stream</div>
        <div className="dl" aria-hidden="true"><i></i></div>
        <button className="play" type="button" aria-label="Запустить демонстрацию нарезки" onClick={run}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3l16 9-16 9z" fill="#0b0b10" /></svg></button>
        <span className="play-hint" aria-hidden="true">жми — покажу весь путь</span>
        <div className="tl" aria-hidden="true">
          <div className="wave-b">
            {Array.from({ length: 64 }, (_, i) => <i key={i} style={{ height: `${18 + Math.abs(Math.sin(i * 1.7) * 60) + (i % 5) * 4}%` }} />)}
            <span className="seg" style={{ left: '12%', width: '9%' }}></span><span className="seg" style={{ left: '44%', width: '12%' }}></span><span className="seg" style={{ left: '76%', width: '8%' }}></span>
          </div>
          <small><span>начало</span><span>агент выбрал 3 момента</span><span>конец</span></small>
        </div>
        <div className="outs" aria-hidden="true">
          {['gta', 'stream', 'pod'].map((s) => <div className="o" key={s}><svg viewBox="0 0 180 320"><use href={`#sc-${s}`} /></svg></div>)}
        </div>
        <button className="replay" type="button" onClick={run}>Ещё раз</button>
      </div>
      <ol className="pipe-l">
        {STEPS.map(([t, s], i) => (
          <li key={t} className={i === n - 1 ? 'on' : i < n - 1 ? 'done' : undefined}><span>{t}<small>{s}</small></span></li>
        ))}
      </ol>
    </div>
  );
}

function BannerDemo({ tick }) {
  const [type, setType] = useState('tg');
  const [bot, setBot] = useState(false);
  const Chip = ({ on, set, color, children }) => (
    <button className="chip" type="button" aria-pressed={on} onClick={set}>{color && <i style={{ background: color }}></i>}{children}</button>
  );
  return (
    <div className="panel bgrid">
      <div className="bphone">
        <Phone scene="game" banner={type} bot={bot} hook="Это должен увидеть каждый" words={['смотри', 'до', 'конца']} tick={tick} />
      </div>
      <div className="ctrl">
        <h2 style={{ marginBottom: 12 }}>Баннер на каждом клипе</h2>
        <p className="muted" style={{ marginBottom: 24, maxWidth: '52ch' }}>Рекламный слот или свой канал: один раз загружаешь файл, и он встаёт на все клипы пачки. Попробуй:</p>
        <h3>Что показываем</h3>
        <div className="chips">
          <Chip on={type === 'tg'} set={() => setType('tg')} color="#2aa3e0">Свой канал</Chip>
          <Chip on={type === 'ad'} set={() => setType('ad')} color="#22c55e">Реклама</Chip>
          <Chip on={type === 'own'} set={() => setType('own')} color="var(--lime)">Свой бренд</Chip>
        </div>
        <h3>Где стоит</h3>
        <div className="chips">
          <Chip on={!bot} set={() => setBot(false)}>Сверху</Chip>
          <Chip on={bot} set={() => setBot(true)}>Снизу, под субтитрами</Chip>
        </div>
        <ul className="bnote">
          <li>PNG, JPG, GIF или MP4: анимация крутится по кругу</li>
          <li>Не налезает на лицо и хук</li>
          <li>Один раз загрузил, и баннер стоит на всей пачке, а не в каждом ролике руками</li>
        </ul>
      </div>
    </div>
  );
}

const PAINS = [
  ['Два дня монтажа на один стрим', 'Пересмотреть запись, отметить моменты, нарезать, поставить в вертикаль. Монтаж съедает больше времени, чем сам эфир.', 'Моменты находит агент, нарезку и кроп делает система'],
  ['Платные ключи и подписки', 'Облачные нарезчики и сервисы с API просят оплату за запросы или минуты видео.', 'Моменты выбирает твой агент (Claude Code или Codex), ключи не нужны'],
  ['Субтитры и баннер — руками', 'Набрать текст, подогнать тайминги, поставить рекламу и проверить, что ничего не залезло на лицо. И так в каждом ролике.', 'Субтитры по словам и баннер сразу на всю пачку'],
];

const FEATS = [
  ['rgba(124,58,237,.25)', '#c4b5fd', <><rect x="2" y="5" width="20" height="14" rx="3" /><path d="M2 10h20M6 15h4" /></>, 'Без API-ключей', 'Claude Code или Codex выбирают моменты. Отдельные платные ключи не нужны.'],
  ['rgba(214,255,61,.16)', 'var(--lime)', <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></>, 'Всё на твоём ПК', 'Расшифровка, кроп, субтитры и баннер идут локально, в Docker.'],
  ['rgba(56,189,248,.18)', 'var(--blue)', <><path d="M4 6h16M4 12h10M4 18h7" /><path d="m16 16 2 2 4-4" /></>, 'Заголовок и хук', 'Агент пишет заголовок и хук к каждому клипу, а субтитры по словам поддерживают кириллицу.'],
  ['rgba(255,90,54,.18)', 'var(--orange)', <><path d="M12 3a9 9 0 1 0 9 9" /><path d="M12 12l5-5" /></>, 'Проекты стримера', 'Игра сверху, вебкамера снизу: раскладка собирается для стрим-записей.'],
];

const INSTALL = [
  ['Docker', <>Поставь Docker Desktop и запусти его.</>],
  ['Запуск', <>В папке проекта выполни <code>docker compose up --build</code> и дождись окончания сборки.</>],
  ['Открой интерфейс', <>Зайди в браузере на <code>localhost:5175</code> и нажми «Открыть нарезку».</>],
  ['Подключи агента', <>Claude Code или Codex выбирают моменты по расшифровке: ключи не нужны.</>],
  ['Ссылки с YouTube', <>Войди в отдельный YouTube-аккаунт в Firefox: система берёт cookies оттуда. Основной аккаунт для этого не используй.</>],
];

const FAQ = [
  ['Нужны платные ключи?', 'Нет. Режим агента работает без ключей: моменты выбирает Claude Code или Codex, всё остальное считается на твоём компьютере.'],
  ['Какие видео подходят?', 'Стримы, фильмы, подкасты и любые длинные видео, ссылкой или своим файлом. Клипы получаются длиной 15–60 секунд, формат 9:16.'],
  ['Субтитры на русском работают?', 'Да, субтитры по словам с кириллицей, плюс хук поверх клипа.'],
  ['Какие баннеры можно ставить?', 'PNG, JPG, GIF и MP4. Файл загружается один раз и встаёт на все клипы пачки.'],
  ['Как скачивать ссылки с YouTube?', 'Войди в отдельный YouTube-аккаунт в Firefox: система использует его cookies. Основной аккаунт лучше не подставлять.'],
  ['Можно резать чужие видео?', 'Права на материал остаются на тебе: режь своё или бери видео с разрешения автора.'],
];

export default function Landing({ onLaunchApp }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (reduceMotion()) return undefined;
    const t = setInterval(() => setTick((k) => k + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const launch = (e) => { e.preventDefault(); onLaunchApp?.(); };

  return (
    <div className="nz" id="top">
      <div aria-hidden="true" dangerouslySetInnerHTML={{ __html: DEFS }} />
      <SiteNav />
      <main id="main">
        {/* HERO */}
        <section className="hero">
          <svg className="blob b1" viewBox="0 0 200 200" aria-hidden="true"><path fill="currentColor" d="M44 -8c40-10 92 8 118 40 22 28 4 62-22 78-20 12-14 40-38 52-30 14-62-8-82-34C-2 104-12 62 4 32 14 12 26-4 44-8z" /><circle cx="168" cy="150" r="10" fill="currentColor" /><circle cx="150" cy="180" r="5" fill="currentColor" /></svg>
          <svg className="blob b2" viewBox="0 0 200 200" aria-hidden="true"><path fill="currentColor" d="M60 30c30-8 50 14 74 22 26 8 54 2 60 30 6 30-24 46-36 70-12 22-34 42-62 36-26-6-30-36-50-54C30 120 0 106 4 78 8 50 32 38 60 30z" /></svg>
          <svg className="blob b3" viewBox="0 0 200 200" aria-hidden="true"><path fill="currentColor" d="M120 10c34 6 62 40 64 78 2 36-16 74-48 94-30 18-70 14-92-12-20-24-8-58 6-84 16-30 36-82 70-76z" /></svg>
          <div className="wrap hero-grid" data-enter>
            <div>
              <div className="hey hand">Хэй,
                <svg viewBox="0 0 60 50" aria-hidden="true"><path d="M4 6c10 26 26 36 48 36m0 0-10-10m10 10-12 6" fill="none" stroke="#f3f3f7" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </div>
              <h1 className="ttl">
                <span className="im">я</span>
                <span className="name">НАРЕЗ</span>
                <span className="sr"> — нарезка длинных видео в вертикальные шортсы</span>
                <svg className="crown" viewBox="0 0 60 44" aria-hidden="true"><path d="M6 38 2 8l16 14L30 2l12 20L58 8l-4 30z" fill="none" stroke="#d6ff3d" strokeWidth="4" strokeLinejoin="round" /></svg>
              </h1>
              <svg className="brush" viewBox="0 0 520 36" aria-hidden="true"><path d="M6 22c80-12 210-18 330-14 60 2 120 6 176 2-40 8-120 12-190 12-110 0-210 2-312 10-8 0-10-8-4-10z" fill="#d6ff3d" /></svg>
              <p className="roles"><span>Нарезка в 9:16</span><span>Субтитры</span><span>Хуки</span><span>Баннеры</span></p>
              <p className="hero-lead">Кидаешь ссылку на стрим, фильм или подкаст — получаешь пачку вертикальных клипов с субтитрами, хуком и твоим баннером. <b>Из 23-минутного стрима — 6 клипов</b> на нашем тесте.</p>
              <div className="cta-row">
                <a className="btn btn-l" href="#app" onClick={launch}>Открыть нарезку <span aria-hidden="true">→</span></a>
                <a className="btn btn-o" href="#install" onClick={scrollTo('install')}>Как установить</a>
              </div>
              <div className="works">
                <span className="w-l">Работает напрямую с</span>
                <span className="w-chip"><i style={{ background: '#d97757' }}></i>Claude Code</span>
                <span className="w-chip"><i style={{ background: '#10a37f' }}></i>Codex</span>
                <p className="w-n">Платные API-ключи не нужны: <b>твой ИИ-агент + твой ПК</b>, всё работает в Docker.</p>
              </div>
            </div>
            <div className="stage">
              <div className="bubble" aria-hidden="true">
                <svg viewBox="0 0 160 130"><path d="M20 10h120c10 0 16 6 16 16v60c0 10-6 16-16 16H70l-26 22 4-22H20c-10 0-16-6-16-16V26c0-10 6-16 16-16z" fill="#38bdf8" stroke="#0b0b10" strokeWidth="4" strokeLinejoin="round" /></svg>
                <p>Давай нарежем вирусное!</p>
              </div>
              <svg className="doodle" style={{ right: '10%', top: '4%', width: 34 }} viewBox="0 0 24 24" aria-hidden="true"><use href="#i-star" style={{ color: 'var(--yellow)' }} /></svg>
              <svg className="doodle" style={{ right: '2%', top: '30%', width: 44 }} viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="16" fill="none" stroke="#d6ff3d" strokeWidth="3" /><circle cx="14" cy="16" r="2" fill="#d6ff3d" /><circle cx="26" cy="16" r="2" fill="#d6ff3d" /><path d="M12 24q8 8 16 0" fill="none" stroke="#d6ff3d" strokeWidth="3" strokeLinecap="round" /></svg>
              <svg className="doodle" style={{ left: '4%', bottom: '14%', width: 34 }} viewBox="0 0 30 40" aria-hidden="true"><path d="M18 2 4 22h10l-4 16 16-22H16z" fill="#d6ff3d" /></svg>
              <svg className="doodle" style={{ left: '30%', top: 0, width: 60 }} viewBox="0 0 60 30" aria-hidden="true"><path d="M4 22c8-14 14 4 22-8s14 6 22-6 8 4 8 4" fill="none" stroke="#ff4fa3" strokeWidth="3" strokeLinecap="round" /></svg>
              <div className="term-chip" aria-hidden="true">
                <div className="tc-h"><i></i><i></i><i></i><span>терминал</span></div>
                <p><b>$ claude</b> нарежь стрим</p>
                <p className="tc-ok">✓ моменты выбраны</p>
                <p className="tc-ok">✓ рендер на ПК</p>
              </div>
              <div className="ribbon ribbon2" aria-hidden="true"><div>{[0, 1].flatMap((k) => ['9:16', 'субтитры', 'хуки', 'баннеры'].map((w) => <span key={`${k}${w}`}>{w}</span>))}</div></div>
              <div className="ribbon" aria-hidden="true"><div>{[0, 1].flatMap((k) => ['игры', 'фильмы', 'стримы', 'подкасты'].map((w) => <span key={`${k}${w}`}>{w}</span>))}</div></div>
              <svg className="char" viewBox="0 0 420 480" role="img" aria-label="Персонаж НАРЕЗ машет рукой и держит телефон с вертикальным клипом"><use href="#mascot" /></svg>
            </div>
          </div>
        </section>

        {/* PAIN */}
        <section className="blk" id="pain">
          <div className="wrap"><div className="panel">
            <div className="head"><h2>Знакомо?</h2><span className="hand" style={{ fontSize: '1.5rem', color: 'var(--orange)', transform: 'rotate(-3deg)' }}>было у каждого</span></div>
            <div className="pains">
              {PAINS.map(([t, p, fix], i) => (
                <article className="pain" key={t}><span className="tag">БОЛЬ #{i + 1}</span><h3>{t}</h3><p>{p}</p><p className="fix">{fix}</p></article>
              ))}
            </div>
          </div></div>
        </section>

        {/* HOW IT WORKS */}
        <section className="blk" id="reel"><div className="wrap"><Reel /></div></section>

        {/* NICHES */}
        <section className="blk" id="niches">
          <div className="wrap"><div className="panel">
            <div className="head">
              <h2>Что нарезаем <svg className="squig" viewBox="0 0 70 20" aria-hidden="true"><path d="M2 12c8-10 14 8 22 0s14 8 22 0 14 8 22 0" fill="none" stroke="#a78bfa" strokeWidth="3" strokeLinecap="round" /></svg></h2>
            </div>
            <div className="niches">
              {NICHES.map((n) => (
                <article className="ncard" key={n.title}>
                  <Phone scene={n.scene} banner={n.banner} bot={n.bot} hook={n.hook} words={n.words} tick={tick} />
                  <div className="nmeta"><div><h3>{n.title}</h3><p>{n.note}</p></div></div>
                </article>
              ))}
            </div>
          </div></div>
        </section>

        <div className="band" aria-hidden="true"><div>
          {[0, 1].flatMap((k) => ['Игры', 'Фильмы', 'Стримы', 'Подкасты', 'Интервью'].map((w) => <span key={`${k}${w}`}>{w}</span>))}
        </div></div>

        {/* BANNERS */}
        <section className="blk" id="banners"><div className="wrap"><BannerDemo tick={tick} /></div></section>

        {/* AGENT */}
        <section className="blk" id="agent">
          <div className="wrap"><div className="panel">
            <div className="head">
              <h2>ИИ-агент вместо ключей</h2>
              <div className="prov"><span><i style={{ background: '#d97757' }}></i>Claude Code</span><span><i style={{ background: '#10a37f' }}></i>Codex</span></div>
            </div>
            <p className="muted" style={{ maxWidth: '62ch' }}>Моменты выбирает ИИ-агент, которым ты уже пользуешься. Он читает расшифровку эфира, находит сильные куски и пишет к ним хуки. Остальное система делает у тебя на компьютере.</p>
            <div className="agrid">
              <div className="chat go">
                <div className="chat-h"><b>AI</b>Агент · пример<small>● работает</small></div>
                <div className="chat-b">
                  <div className="msg me">Найди в стриме моменты для шортсов</div>
                  <div className="msg ai"><span className="mono">читаю transcript.json</span></div>
                  <div className="msg ai">
                    <div className="moment"><code>клип 1</code><span>«Чат не поверил»</span><em>15–60 с</em></div>
                    <div className="moment"><code>клип 2</code><span>«Это был баг?»</span><em>15–60 с</em></div>
                    <div className="moment"><code>клип 3</code><span>«Последний шанс»</span><em>15–60 с</em></div>
                  </div>
                  <div className="msg ai"><span className="ok">clips.json записан ✓</span> Система режет и оформляет клипы.</div>
                </div>
              </div>
              <div className="feats">
                {FEATS.map(([bg, color, icon, t, p]) => (
                  <article className="feat" key={t}>
                    <div className="ic" style={{ background: bg, color }}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{icon}</svg></div>
                    <h3>{t}</h3><p>{p}</p>
                  </article>
                ))}
              </div>
            </div>
          </div></div>
        </section>

        {/* INSTALL */}
        <section className="blk" id="install">
          <div className="wrap"><div className="panel">
            <div className="head"><h2>Установка</h2><span className="hand" style={{ fontSize: '1.5rem', color: 'var(--lime)', transform: 'rotate(-3deg)' }}>пять шагов</span></div>
            <ol className="steps2">
              {INSTALL.map(([t, p]) => <li key={t}><h3>{t}</h3><p>{p}</p></li>)}
            </ol>
            <div className="cta-row">
              <a className="btn btn-l" href="#app" onClick={launch}>Открыть нарезку <span aria-hidden="true">→</span></a>
            </div>
          </div></div>
        </section>

        {/* FAQ */}
        <section className="blk" id="faq">
          <div className="wrap"><div className="panel">
            <div className="head"><h2>Коротко о главном</h2></div>
            <div className="faq">
              {FAQ.map(([q, a]) => <details key={q}><summary>{q}</summary><p>{a}</p></details>)}
            </div>
          </div></div>
        </section>
      </main>

      <footer className="foot">
        <div className="wrap">
          <div className="fbrand">
            <span className="logo">НАРЕЗ<b>.</b></span>
            <p>Длинное видео — в вертикальные клипы с субтитрами, хуком и баннером.</p>
            <a className="btn btn-l" href="#app" onClick={launch}>Открыть нарезку <span aria-hidden="true">→</span></a>
          </div>
        </div>
      </footer>
    </div>
  );
}
