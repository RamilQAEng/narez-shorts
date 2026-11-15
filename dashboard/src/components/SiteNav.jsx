// Шапка главной страницы НАРЕЗ. Стили в ../landing.css (область .nz).
// Ссылки-разделы прокручивают страницу, не меняя hash: hash занят роутером main.jsx.
import { useState } from 'react';

const LINKS = [
  ['reel', 'Как работает'],
  ['niches', 'Направления'],
  ['banners', 'Баннеры'],
  ['agent', 'ИИ-агент'],
  ['install', 'Установка'],
  ['faq', 'Вопросы'],
];

export default function SiteNav({ className = '' }) {
  const [open, setOpen] = useState(false);
  const go = (id) => (e) => {
    e.preventDefault();
    setOpen(false);
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' });
  };
  return (
    <header className={`top ${className}`}>
      <div className="wrap bar">
        <a className="logo" href="#top" onClick={(e) => { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }); }}>НАРЕЗ<b>.</b></a>
        <nav className={`links${open ? ' open' : ''}`} id="menu" aria-label="Разделы">
          {LINKS.map(([id, label]) => (
            <a key={id} href={`#${id}`} onClick={go(id)}>{label}</a>
          ))}
        </nav>
        <a className="btn btn-l nav-cta" href="#app">Открыть нарезку</a>
        <button className="burger" type="button" aria-expanded={open} aria-controls="menu" aria-label="Меню" onClick={() => setOpen(!open)}><span></span></button>
      </div>
    </header>
  );
}
