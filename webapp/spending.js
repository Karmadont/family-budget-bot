/*
  spending.js — экран «Траты»: лента покупок, правка записи, ответы на вопросы.

  Смысл экрана — исправлять. Нейросеть иногда кладёт покупку не в ту категорию
  или неверно читает цену, и до этого экрана поправить её было нельзя: только
  удалить последнюю запись через /undo. Поэтому нажатие на любую строку
  открывает форму, а не просто показывает подробности.
*/

import { amount, dayTitle, escapeHtml, money, plural } from './format.js';
import { api, state, tg } from './store.js';

const el = (id) => document.getElementById(id);

const SEARCH_DEBOUNCE_MS = 350;

const filters = { period: '', review: false, search: '' };

let items = [];
let cursor = null;
let editing = null;      // запись, открытая в форме правки
let searchTimer = null;
let onReviewChange = null;   // сообщить приложению, что счётчик изменился

export function mount({ onReview }) {
  onReviewChange = onReview;

  el('feed-period').addEventListener('change', (event) => {
    filters.period = event.target.value;
    reload();
  });

  el('feed-review').addEventListener('click', () => {
    filters.review = !filters.review;
    el('feed-review').setAttribute('aria-pressed', String(filters.review));
    reload();
  });

  el('feed-search').addEventListener('input', (event) => {
    // Каждое нажатие клавиши — это запрос к серверу, поэтому ждём паузы.
    clearTimeout(searchTimer);
    const value = event.target.value.trim();
    searchTimer = setTimeout(() => {
      filters.search = value;
      reload();
    }, SEARCH_DEBOUNCE_MS);
  });

  el('feed-more').addEventListener('click', () => load({ append: true }));

  el('feed').addEventListener('click', (event) => {
    const row = event.target.closest('[data-id]');
    if (row) openEditor(+row.dataset.id);
  });

  el('pending').addEventListener('submit', answerPending);

  el('edit-form').addEventListener('submit', save);
  el('edit-cancel').addEventListener('click', closeEditor);
  el('edit-delete').addEventListener('click', remove);
  el('sheet').addEventListener('click', (event) => {
    // Клик по затемнению — закрыть; клик внутри формы — нет.
    if (event.target === el('sheet')) closeEditor();
  });
}

/** Экран открыли: если данных ещё нет — загрузить. */
export async function show() {
  if (!items.length) await reload();
}

export async function reload() {
  cursor = null;
  items = [];
  await load({ append: false });
  await loadPending();
}

async function load({ append }) {
  const screen = el('screen-spending');
  if (append) el('feed-more').disabled = true;
  else if (items.length) screen.classList.add('busy');

  try {
    const page = await api('/api/purchases', {
      params: {
        period: filters.period,
        review: filters.review ? 'true' : '',
        search: filters.search,
        cursor: append ? cursor : '',
      },
    });
    items = append ? [...items, ...page.items] : page.items;
    cursor = page.next;
    setReview(page.review);
    render();
  } finally {
    screen.classList.remove('busy');
    el('feed-more').disabled = false;
  }
}

function setReview(count) {
  state.review = count ?? 0;
  el('feed-review').textContent = state.review
    ? `Проверить ${state.review}`
    : 'Проверить';
  el('feed-review').disabled = !state.review && !filters.review;
  onReviewChange?.(state.review);
}

// --- лента --------------------------------------------------------------------

function render() {
  const feed = el('feed');

  if (!items.length) {
    feed.innerHTML = `<p class="empty">${
      filters.review ? 'Нечего проверять — все записи в порядке.'
      : filters.search ? 'Ничего не нашлось.'
      : 'Покупок пока нет. Напишите в чат, что купили и почём.'
    }</p>`;
    el('feed-more').hidden = true;
    return;
  }

  // Группируем по дням: без заголовков лента из сотни строк не читается.
  const groups = [];
  for (const item of items) {
    const last = groups.at(-1);
    if (last && last.day === item.bought_at) last.items.push(item);
    else groups.push({ day: item.bought_at, items: [item] });
  }

  feed.innerHTML = groups.map((group) => {
    const sum = group.items.reduce((total, item) => total + item.price, 0);
    return `
      <div class="day">
        <div class="day-head">
          <span>${escapeHtml(dayTitle(group.day))}</span>
          <span>${money(sum)}</span>
        </div>
        ${group.items.map(rowHtml).join('')}
      </div>`;
  }).join('');

  el('feed-more').hidden = !cursor;
}

function rowHtml(item) {
  const qty = amount(item);
  const meta = [item.category, qty, item.store].filter(Boolean).map(escapeHtml).join(' · ');
  return `
    <button class="row" data-id="${item.id}" type="button">
      <span class="row-main">
        <span class="row-name">${escapeHtml(item.name)}${
          item.needs_review ? '<span class="flag" title="Проверьте категорию">❓</span>' : ''
        }</span>
        <span class="row-meta">${meta}</span>
      </span>
      <span class="row-sum">${money(item.price)}</span>
    </button>`;
}

// --- уточняющие вопросы -------------------------------------------------------

async function loadPending() {
  const box = el('pending');
  try {
    const { items: questions } = await api('/api/pending');
    box.innerHTML = questions.map((q) => `
      <form class="ask" data-id="${q.id}">
        <p class="ask-question">❓ ${escapeHtml(q.question)}</p>
        <p class="ask-source">${escapeHtml(q.raw_text)}</p>
        <div class="ask-row">
          <input name="answer" placeholder="Ответ" required maxlength="1000" autocomplete="off">
          <button type="submit">Ответить</button>
        </div>
      </form>`).join('');
    box.hidden = !questions.length;
  } catch {
    // Вопросы — дополнение к ленте. Если не пришли, экран должен работать дальше.
    box.hidden = true;
  }
}

async function answerPending(event) {
  event.preventDefault();
  const form = event.target.closest('.ask');
  if (!form) return;

  const input = form.querySelector('input[name="answer"]');
  const answer = input.value.trim();
  if (!answer) return;

  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const result = await api(`/api/pending/${form.dataset.id}/answer`, {
      method: 'POST',
      body: { answer },
    });
    toast(result.saved
      ? `Записал ${result.saved} ${plural(result.saved, 'позицию', 'позиции', 'позиций')}`
      : (result.message || 'Не вышло разобрать ответ'));
    await reload();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

// --- правка записи ------------------------------------------------------------

function openEditor(id) {
  editing = items.find((item) => item.id === id);
  if (!editing) return;

  const form = el('edit-form');
  form.name.value = editing.name;
  form.price.value = editing.price;
  form.quantity.value = editing.quantity ?? '';
  form.unit.value = editing.unit ?? '';
  form.bought_at.value = editing.bought_at;

  form.category.innerHTML = state.categories
    .map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`)
    .join('');
  form.category.value = editing.category;

  el('edit-source').textContent = editing.source === 'import'
    ? 'Запись из импорта истории'
    : editing.source === 'receipt' ? 'Запись из фото чека' : '';
  el('edit-error').hidden = true;
  el('sheet').hidden = false;

  // Аппаратная «назад» должна закрывать форму, а не выходить из приложения.
  tg?.BackButton?.show?.();
  tg?.BackButton?.onClick?.(closeEditor);
}

function closeEditor() {
  el('sheet').hidden = true;
  editing = null;
  tg?.BackButton?.offClick?.(closeEditor);
  tg?.BackButton?.hide?.();
}

async function save(event) {
  event.preventDefault();
  if (!editing) return;

  const form = el('edit-form');
  const changes = {
    name: form.name.value.trim(),
    category: form.category.value,
    price: Number(form.price.value),
    // Пустое поле означает «неизвестно», а не ноль: сервер такое обнуляет.
    quantity: form.quantity.value === '' ? null : Number(form.quantity.value),
    unit: form.unit.value.trim() || null,
    bought_at: form.bought_at.value,
  };

  if (!changes.name) return showEditError('Впишите название.');
  if (!Number.isFinite(changes.price) || changes.price < 0) {
    return showEditError('Сумма должна быть числом не меньше нуля.');
  }

  const button = el('edit-save');
  button.disabled = true;
  try {
    const result = await api(`/api/purchases/${editing.id}`, { method: 'PATCH', body: changes });
    // Обновляем строку на месте: перезагружать всю ленту ради одной правки
    // значило бы терять место прокрутки.
    const index = items.findIndex((item) => item.id === editing.id);
    if (index !== -1 && result.item) items[index] = result.item;
    setReview(result.review);
    render();
    closeEditor();
    toast('Поправил');
  } catch (error) {
    showEditError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function remove() {
  if (!editing) return;
  if (!await confirmDelete(editing.name)) return;

  const id = editing.id;
  try {
    const result = await api(`/api/purchases/${id}`, { method: 'DELETE' });
    items = items.filter((item) => item.id !== id);
    setReview(result.review);
    render();
    closeEditor();
    toast('Удалил');
  } catch (error) {
    showEditError(error.message);
  }
}

function showEditError(text) {
  const box = el('edit-error');
  box.textContent = text;
  box.hidden = false;
}

/** Подтверждение удаления: у Telegram диалог родной, в браузере — обычный. */
function confirmDelete(name) {
  const question = `Удалить «${name}»?`;
  if (tg?.showConfirm) {
    return new Promise((resolve) => tg.showConfirm(question, resolve));
  }
  return Promise.resolve(window.confirm(question));
}

let toastTimer = null;

function toast(text) {
  const box = el('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 2500);
}
