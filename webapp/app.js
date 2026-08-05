/*
  app.js — вся логика мини-приложения.

  Обычный JavaScript без сборки: файл можно открыть и прочитать сверху вниз,
  а чтобы увидеть правку — достаточно обновить страницу.

  Главное, что нужно понимать про Telegram: объект Telegram.WebApp.initData —
  это строка с данными пользователя и подписью. Мы отправляем её на сервер при
  каждом запросе, и сервер по ней понимает, кто пришёл. Рядом лежит
  initDataUnsafe — то же самое, но уже разобранное и БЕЗ проверки подписи;
  подставить туда чужой id может кто угодно, поэтому решения по нему не
  принимаются никогда.
*/

const tg = window.Telegram?.WebApp;

const state = {
  chat: null,        // токен чата из ссылки либо id строкой
  period: 'month',
  currency: '₽',
  expanded: false,   // показаны ли все категории
  data: null,
};

const el = (id) => document.getElementById(id);


/* --- запросы к серверу ----------------------------------------------------- */

async function api(path, params = {}) {
  const url = new URL(path, location.href);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) url.searchParams.set(key, value);
  }

  const response = await fetch(url, {
    headers: { Authorization: `tma ${tg?.initData ?? ''}` },
  });

  let body = null;
  try {
    body = await response.json();
  } catch {
    // Сервер упал так, что не осилил JSON — покажем хотя бы код ответа.
  }
  if (!response.ok) throw new Error(body?.error || `Сервер ответил ${response.status}`);
  return body;
}


/* --- форматирование -------------------------------------------------------- */

const nf0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function money(value) {
  return `${nf0.format(Math.round(value))} ${state.currency}`;
}

/** Компактно для осей и подписей: 1 234 -> 1,2 тыс. */
function compact(value) {
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}т`;
  return nf0.format(Math.round(value));
}

// Два списка не от лени: у даты месяц стоит в родительном падеже («5 мая»),
// а сам по себе — в именительном («май 26»). Различается только май, но
// «мая 26» в подписи оси читается как опечатка.
const MONTHS_OF = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн',
                   'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
                'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

function labelFor(at, step) {
  const [year, month, day] = at.split('-');
  return step === 'month'
    ? `${MONTHS[+month - 1]} ${year.slice(2)}`
    : `${+day} ${MONTHS_OF[+month - 1]}`;
}

/** Русское склонение: 1 позиция, 2 позиции, 5 позиций. */
function plural(n, one, few, many) {
  const tail = Math.abs(n) % 100;
  if (tail >= 11 && tail <= 14) return many;
  switch (tail % 10) {
    case 1: return one;
    case 2: case 3: case 4: return few;
    default: return many;
  }
}


/* --- график по дням -------------------------------------------------------- */

// maxBar — потолок ширины столбика. Без него неделя из пяти дней рисуется
// пятью широкими плашками: читается как заливка, а не как данные.
const CHART = { width: 320, plot: 110, top: 6, axis: 22, gap: 2, radius: 4, maxBar: 26 };

/**
 * Столбик с закруглённой верхушкой и прямым основанием.
 *
 * Скруглить rect'ом целиком нельзя: у столбика, стоящего на оси, низ должен
 * лежать на линии, а не отрываться от неё скруглением.
 */
function barPath(x, y, w, h) {
  const r = Math.min(CHART.radius, w / 2, h);
  const bottom = y + h;
  return `M${x} ${bottom} L${x} ${y + r} Q${x} ${y} ${x + r} ${y}`
       + ` L${x + w - r} ${y} Q${x + w} ${y} ${x + w} ${y + r} L${x + w} ${bottom} Z`;
}

/**
 * Достроить пустые дни и месяцы.
 *
 * База отдаёт только то, где были траты. Если рисовать как есть, три покупки
 * за полгода встанут тремя соседними столбиками — и график скажет «тратим
 * каждый месяц примерно поровну» вместо правды.
 */
function fillGaps(points, period) {
  const known = new Map(points.map((p) => [p.at, p.total]));
  const result = [];
  const cursor = new Date(`${period.since}T00:00:00Z`);
  const last = new Date(`${period.until}T00:00:00Z`);
  const monthly = period.step === 'month';
  // Шагать по месяцам с 31-го числа нельзя: +1 месяц от 31 января даёт 3 марта,
  // и февраль потерялся бы. Встаём на первое число.
  if (monthly) cursor.setUTCDate(1);

  // Ограничение сверху — на случай, если период вдруг окажется огромным:
  // тысяча столбиков всё равно не нарисуется, а вкладку подвесит.
  while (cursor <= last && result.length < 400) {
    const at = cursor.toISOString().slice(0, monthly ? 7 : 10);
    result.push({ at, total: known.get(at) ?? 0 });
    if (monthly) cursor.setUTCMonth(cursor.getUTCMonth() + 1);
    else cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return result;
}

function renderTimeline(points, period) {
  const box = el('timeline');
  const caption = el('timeline-caption');
  const series = fillGaps(points, period);

  if (!series.length) {
    box.innerHTML = '';
    caption.textContent = '';
    return;
  }

  const { width, plot, top, axis, gap } = CHART;
  const height = top + plot + axis;
  const max = Math.max(...series.map((p) => p.total), 1);
  const slot = width / series.length;
  const barWidth = Math.min(CHART.maxBar, Math.max(1.5, slot - gap));

  // Прямая подпись — только для максимума: число над каждым столбиком читать
  // невозможно, а «самый дорогой день» — как раз то, что ищут глазами.
  const peak = series.reduce((a, b) => (b.total > a.total ? b : a));

  const bars = series.map((point, index) => {
    const barHeight = point.total > 0 ? Math.max(2, (point.total / max) * plot) : 0;
    const x = index * slot + (slot - barWidth) / 2;
    const y = top + plot - barHeight;
    const shape = barHeight
      ? `<path class="bar" d="${barPath(x, y, barWidth, barHeight)}"/>`
      : '';
    // Отдельная прозрачная зона нажатия во всю высоту: попасть пальцем в
    // столбик шириной пять пикселей невозможно.
    return shape + `<rect class="hit" x="${index * slot}" y="0" width="${slot}"
                          height="${top + plot}" data-index="${index}"/>`;
  }).join('');

  const ticks = axisTicks(series, period.step, slot);

  box.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img"
         aria-label="Траты по ${period.step === 'month' ? 'месяцам' : 'дням'}">
      <line class="rule" x1="0" y1="${top + plot}" x2="${width}" y2="${top + plot}"/>
      ${bars}
      ${ticks}
    </svg>`;

  const show = (point) => {
    caption.textContent = point.total
      ? `${labelFor(point.at, period.step)} — ${money(point.total)}`
      : `${labelFor(point.at, period.step)} — трат нет`;
  };
  show(peak);

  box.querySelectorAll('.hit').forEach((zone) => {
    zone.addEventListener('click', () => {
      const point = series[+zone.dataset.index];
      show(point);
      box.querySelectorAll('.bar').forEach((bar) => bar.classList.add('dim'));
      const bars = box.querySelectorAll('.bar');
      // Столбики нулевой высоты не рисуются, поэтому индекс в DOM свой.
      let visible = -1;
      series.forEach((p, i) => {
        if (p.total > 0) visible += 1;
        if (i === +zone.dataset.index && p.total > 0) bars[visible]?.classList.remove('dim');
      });
      tg?.HapticFeedback?.selectionChanged?.();
    });
  });
}

/** Подписи оси X: 4–5 штук, иначе они наедут друг на друга. */
function axisTicks(series, step, slot) {
  const wanted = Math.min(5, series.length);
  const every = Math.max(1, Math.round(series.length / wanted));
  const y = CHART.top + CHART.plot + 14;

  return series.map((point, index) => {
    if (index % every !== 0) return '';
    const x = index * slot + slot / 2;
    // Крайние подписи прижимаем к краям, чтобы не вылезали за viewBox.
    const anchor = x < 24 ? 'start' : x > CHART.width - 24 ? 'end' : 'middle';
    const at = anchor === 'start' ? 0 : anchor === 'end' ? CHART.width : x;
    return `<text class="axis" x="${at}" y="${y}" text-anchor="${anchor}">${labelFor(point.at, step)}</text>`;
  }).join('');
}


/* --- отрисовка экрана ------------------------------------------------------ */

const CATEGORIES_SHOWN = 8;

function renderCategories(categories) {
  const box = el('categories');
  const more = el('categories-more');

  if (!categories.length) {
    box.innerHTML = '<p class="empty">Покупок за этот период нет.</p>';
    more.hidden = true;
    return;
  }

  const max = categories[0].total;
  const visible = state.expanded ? categories : categories.slice(0, CATEGORIES_SHOWN);

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
  more.textContent = state.expanded
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
  box.textContent = `${diff > 0 ? '↑' : '↓'} ${Math.abs(percent)}% ${previous.label} — ${money(previous.total)}`;
}

function render(data) {
  state.data = data;

  el('chat-title').textContent = data.chat.title;
  el('total').textContent = money(data.total);
  el('subtitle').textContent = data.items
    ? `${data.period.label} · ${data.items} ${plural(data.items, 'позиция', 'позиции', 'позиций')}`
    : data.period.label;

  renderDelta(data.previous, data.total);
  renderCategories(data.categories);
  renderTimeline(data.timeline, data.period);
  renderTop(data.top);

  el('card-timeline').hidden = !data.items;
  el('card-top').hidden = !data.items;
}

/** Экранирование пользовательских строк: названия товаров идут в innerHTML. */
function escapeHtml(text) {
  const node = document.createElement('span');
  node.textContent = text;
  return node.innerHTML;
}


/* --- загрузка -------------------------------------------------------------- */

function showError(message) {
  el('loading').hidden = true;
  el('screen').hidden = true;
  el('error').hidden = false;
  el('error-text').textContent = message;
}

async function load() {
  const screen = el('screen');
  // Данные уже показаны — не подменяем их скелетом, просто приглушаем.
  if (state.data) screen.classList.add('busy');

  try {
    const data = await api('/api/overview', { chat: state.chat, period: state.period });
    render(data);
    el('loading').hidden = true;
    el('error').hidden = true;
    screen.hidden = false;
  } catch (error) {
    showError(error.message);
  } finally {
    screen.classList.remove('busy');
  }
}

async function boot() {
  if (!tg) {
    showError('Это приложение открывается внутри Telegram.');
    return;
  }

  tg.ready();
  tg.expand();
  applyTheme();
  tg.onEvent('themeChanged', applyTheme);

  if (!tg.initData) {
    showError('Telegram не передал данные авторизации. Откройте приложение через кнопку бота.');
    return;
  }

  // Чат приходит в ссылке (?startapp=…) и попадает сюда как start_param.
  state.chat = tg.initDataUnsafe?.start_param ?? null;

  try {
    const me = await api('/api/me');
    state.currency = me.currency || state.currency;

    if (!me.chats.length) {
      showError('Не нашёл ни одного чата с вашими покупками. Добавьте бота в чат и напишите, что купили.');
      return;
    }
    if (!state.chat) state.chat = me.chats[0].token;
    setupChatPicker(me.chats);
  } catch (error) {
    showError(error.message);
    return;
  }

  await load();
}

function setupChatPicker(chats) {
  if (chats.length < 2) return;
  const picker = el('chat-picker');
  picker.innerHTML = chats
    .map((chat) => `<option value="${chat.token}">${escapeHtml(chat.title)}</option>`)
    .join('');
  picker.value = chats.find((chat) => chat.token === state.chat)?.token ?? chats[0].token;
  state.chat = picker.value;
  picker.hidden = false;
  picker.addEventListener('change', () => {
    state.chat = picker.value;
    state.expanded = false;
    load();
  });
}

function applyTheme() {
  document.documentElement.dataset.theme = tg.colorScheme === 'dark' ? 'dark' : 'light';
  try {
    tg.setHeaderColor('bg_color');
  } catch {
    // Старые клиенты этого не умеют — не беда, шапка просто останется своей.
  }
}


/* --- события --------------------------------------------------------------- */

el('periods').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-period]');
  if (!button || button.dataset.period === state.period) return;

  for (const tab of el('periods').children) {
    tab.setAttribute('aria-selected', String(tab === button));
  }
  state.period = button.dataset.period;
  state.expanded = false;
  load();
});

el('categories-more').addEventListener('click', () => {
  state.expanded = !state.expanded;
  renderCategories(state.data.categories);
});

el('retry').addEventListener('click', boot);

boot();
