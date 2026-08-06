/*
  chart.js — график трат по дням или месяцам.

  Рисуем SVG руками: столбиковая диаграмма — это десяток строк арифметики,
  а библиотека графиков потянула бы за собой сборку, которой в проекте нет.

  Все столбики одного цвета намеренно. Высота уже показывает величину, и
  раскрашивать их вразнобой значило бы сказать то же самое второй раз.
*/

import { labelFor, money } from './format.js';
import { tg } from './store.js';

const CHART = {
  width: 320,   // система координат viewBox; на экране растягивается по ширине
  plot: 110,    // высота области столбиков
  top: 6,
  axis: 22,     // полоса под подписи оси — иначе они обрежутся
  gap: 2,       // просвет между столбиками
  radius: 4,
  maxBar: 26,   // потолок ширины: неделя из пяти дней иначе рисуется плашками
};

/**
 * Столбик с закруглённой верхушкой и прямым основанием.
 *
 * Скруглить rect'ом целиком нельзя: столбик стоит на оси, и низ должен лежать
 * на линии, а не отрываться от неё скруглением.
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
 * База отдаёт только то, где были траты. Если рисовать как есть, три покупки за
 * полгода встанут тремя соседними столбиками — и график скажет «тратим каждый
 * месяц примерно поровну» вместо правды.
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

  // Предохранитель: тысяча столбиков всё равно не нарисуется, а вкладку подвесит.
  while (cursor <= last && result.length < 400) {
    const at = cursor.toISOString().slice(0, monthly ? 7 : 10);
    result.push({ at, total: known.get(at) ?? 0 });
    if (monthly) cursor.setUTCMonth(cursor.getUTCMonth() + 1);
    else cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return result;
}

/** Подписи оси X: 4–5 штук, иначе они наедут друг на друга. */
function axisTicks(series, step, slot) {
  const wanted = Math.min(5, series.length);
  const every = Math.max(1, Math.round(series.length / wanted));
  const y = CHART.top + CHART.plot + 14;

  return series.map((point, index) => {
    if (index % every !== 0) return '';
    const middle = index * slot + slot / 2;
    // Крайние подписи прижимаем к краям, чтобы не вылезли за viewBox.
    const anchor = middle < 24 ? 'start' : middle > CHART.width - 24 ? 'end' : 'middle';
    const x = anchor === 'start' ? 0 : anchor === 'end' ? CHART.width : middle;
    return `<text class="axis" x="${x}" y="${y}" text-anchor="${anchor}">`
         + `${labelFor(point.at, step)}</text>`;
  }).join('');
}

export function renderTimeline(box, caption, points, period) {
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

  const bars = series.map((point, index) => {
    const barHeight = point.total > 0 ? Math.max(2, (point.total / max) * plot) : 0;
    const x = index * slot + (slot - barWidth) / 2;
    const shape = barHeight
      ? `<path class="bar" d="${barPath(x, top + plot - barHeight, barWidth, barHeight)}"/>`
      : '';
    // Прозрачная зона нажатия во всю высоту: попасть пальцем в столбик
    // шириной пять пикселей невозможно.
    return `${shape}<rect class="hit" x="${index * slot}" y="0" width="${slot}"`
         + ` height="${top + plot}" data-index="${index}"/>`;
  }).join('');

  box.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img"
         aria-label="Траты по ${period.step === 'month' ? 'месяцам' : 'дням'}">
      <line class="rule" x1="0" y1="${top + plot}" x2="${width}" y2="${top + plot}"/>
      ${bars}
      ${axisTicks(series, period.step, slot)}
    </svg>`;

  // Прямая подпись — только для максимума: число над каждым столбиком читать
  // невозможно, а «самый дорогой день» — как раз то, что ищут глазами.
  const show = (point) => {
    caption.textContent = point.total
      ? `${labelFor(point.at, period.step)} — ${money(point.total)}`
      : `${labelFor(point.at, period.step)} — трат нет`;
  };
  show(series.reduce((a, b) => (b.total > a.total ? b : a)));

  const barNodes = [...box.querySelectorAll('.bar')];
  // Столбики нулевой высоты не рисуются, поэтому DOM-индексы не совпадают с
  // индексами дней. Считаем соответствие один раз, а не при каждом нажатии.
  const barByIndex = new Map();
  let drawn = 0;
  series.forEach((point, index) => {
    if (point.total > 0) barByIndex.set(index, barNodes[drawn++]);
  });

  box.querySelectorAll('.hit').forEach((zone) => {
    zone.addEventListener('click', () => {
      const index = +zone.dataset.index;
      show(series[index]);
      barNodes.forEach((bar) => bar.classList.add('dim'));
      barByIndex.get(index)?.classList.remove('dim');
      tg?.HapticFeedback?.selectionChanged?.();
    });
  });
}
