/*
  format.js — превращение чисел и дат в русский текст.

  Сервер отдаёт сырые числа, а форматирует их приложение: одни и те же 904
  показываются то как «904 ₽» в заголовке, то как «904» на оси графика.
*/

import { state } from './store.js';

const nf0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** 1234 -> «1 234 ₽», 1234.5 -> «1 234,50 ₽» */
export function money(value) {
  const body = Math.abs(value - Math.round(value)) < 0.005
    ? nf0.format(Math.round(value))
    : nf2.format(value);
  return `${body} ${state.currency}`;
}

/** Два списка: у даты месяц в родительном падеже («5 мая»), сам по себе — в именительном. */
const MONTHS_OF = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн',
                   'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
                'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTHS_FULL = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
                     'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

/** Подпись для оси графика: '2026-08-05' -> «5 авг», '2026-08' -> «авг 26». */
export function labelFor(at, step) {
  const [year, month, day] = at.split('-');
  return step === 'month'
    ? `${MONTHS[+month - 1]} ${year.slice(2)}`
    : `${+day} ${MONTHS_OF[+month - 1]}`;
}

/** Заголовок дня в ленте: «сегодня», «вчера» или «5 августа». */
export function dayTitle(iso) {
  const today = new Date();
  const shift = Math.round(
    (Date.parse(`${iso}T00:00:00`) - Date.parse(`${isoDate(today)}T00:00:00`)) / 86400000,
  );
  if (shift === 0) return 'сегодня';
  if (shift === -1) return 'вчера';

  const [year, month, day] = iso.split('-');
  const tail = +year === today.getFullYear() ? '' : ` ${year}`;
  return `${+day} ${MONTHS_FULL[+month - 1]}${tail}`;
}

export function isoDate(date) {
  // toISOString() переводит в UTC и в московском вечере даёт вчерашнее число.
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 10);
}

/** Русское склонение: 1 позиция, 2 позиции, 5 позиций. */
export function plural(n, one, few, many) {
  const tail = Math.abs(n) % 100;
  if (tail >= 11 && tail <= 14) return many;
  switch (tail % 10) {
    case 1: return one;
    case 2: case 3: case 4: return few;
    default: return many;
  }
}

/** «2 кг» или пустая строка — количество позиции, если оно известно. */
export function amount(item) {
  if (item.quantity === null || item.quantity === undefined) return '';
  const qty = Number(item.quantity).toLocaleString('ru-RU', { maximumFractionDigits: 3 });
  return item.unit ? `${qty} ${item.unit}` : qty;
}

/** Экранирование: названия товаров пишет человек, а они идут в innerHTML. */
export function escapeHtml(text) {
  const node = document.createElement('span');
  node.textContent = text ?? '';
  return node.innerHTML;
}
