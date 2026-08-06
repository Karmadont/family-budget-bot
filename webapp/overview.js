/*
  overview.js — экран «Обзор»: сумма за период, категории, график, топ покупок.
*/

import { renderTimeline } from './chart.js';
import { escapeHtml, money, plural } from './format.js';
import { api, state } from './store.js';

const el = (id) => document.getElementById(id);

// Сколько категорий показывать, прежде чем свернуть остальные под кнопку.
const CATEGORIES_SHOWN = 8;

let data = null;
let expanded = false;

export function mount() {
  el('periods').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-period]');
    if (!button || button.dataset.period === state.period) return;

    for (const tab of el('periods').children) {
      tab.setAttribute('aria-selected', String(tab === button));
    }
    state.period = button.dataset.period;
    expanded = false;
    load();
  });

  el('categories-more').addEventListener('click', () => {
    expanded = !expanded;
    renderCategories(data.categories);
  });
}

export async function load() {
  const screen = el('screen-overview');
  // Данные уже показаны — не подменяем их скелетом, просто приглушаем:
  // иначе вёрстка дёргается на каждом переключении периода.
  if (data) screen.classList.add('busy');
  try {
    data = await api('/api/overview', { params: { period: state.period } });
    state.review = data.review ?? state.review;
    render();
  } finally {
    screen.classList.remove('busy');
  }
}

function render() {
  el('chat-title').textContent = data.chat.title;
  el('total').textContent = money(data.total);
  el('subtitle').textContent = data.items
    ? `${data.period.label} · ${data.items} ${plural(data.items, 'позиция', 'позиции', 'позиций')}`
    : data.period.label;

  renderDelta(data.previous, data.total);
  renderCategories(data.categories);
  renderTimeline(el('timeline'), el('timeline-caption'), data.timeline, data.period);
  renderTop(data.top);

  el('card-timeline').hidden = !data.items;
  el('card-top').hidden = !data.items;
}

function renderDelta(previous, total) {
  const box = el('delta');
  if (!previous || !previous.total) {
    box.hidden = true;
    return;
  }
  const diff = total - previous.total;
  const percent = Math.round((diff / previous.total) * 100);
  box.hidden = false;
  box.dataset.dir = diff > 0 ? 'up' : 'down';
  box.textContent = `${diff > 0 ? '↑' : '↓'} ${Math.abs(percent)}% `
                  + `${previous.label} — ${money(previous.total)}`;
}

function renderCategories(categories) {
  const box = el('categories');
  const more = el('categories-more');

  if (!categories.length) {
    box.innerHTML = '<p class="empty">Покупок за этот период нет.</p>';
    more.hidden = true;
    return;
  }

  const max = categories[0].total;
  const visible = expanded ? categories : categories.slice(0, CATEGORIES_SHOWN);

  box.innerHTML = visible.map((item) => `
    <div class="bar-row">
      <div class="bar-head">
        <span class="bar-name">${escapeHtml(item.name)}</span>
        <span class="bar-value">${money(item.total)}</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill" style="width: ${Math.max(2, (item.total / max) * 100)}%"></div>
      </div>
    </div>`).join('');

  const hidden = categories.length - visible.length;
  more.hidden = categories.length <= CATEGORIES_SHOWN;
  more.textContent = expanded
    ? 'Свернуть'
    : `Ещё ${hidden} ${plural(hidden, 'категория', 'категории', 'категорий')}`;
}

function renderTop(items) {
  el('top').innerHTML = items.length
    ? items.map((item) => `
        <li>
          <span class="name">${escapeHtml(item.name)}
            <span class="count">· ${item.count} ${plural(item.count, 'раз', 'раза', 'раз')}</span>
          </span>
          <span class="sum">${money(item.total)}</span>
        </li>`).join('')
    : '<li class="empty">Пока пусто.</li>';
}
