/*
  store.js — общее состояние и запросы к серверу.

  Здесь живёт всё, что нужно сразу нескольким экранам: кто мы, какой чат
  смотрим, список категорий для правки. Экраны состояние читают, а меняют
  через свои обработчики — отдельного «фреймворка» тут нет и не нужно.
*/

export const tg = window.Telegram?.WebApp;

export const state = {
  chat: null,          // токен чата из ссылки либо id строкой
  period: 'month',     // период экрана «Обзор»
  currency: '₽',
  categories: [],      // для выпадающего списка при правке
  review: 0,           // сколько записей ждут проверки
  user: null,
};

/**
 * Запрос к API.
 *
 * Подпись Telegram уходит в заголовке Authorization при каждом вызове: сервер
 * не держит сессий, и каждый запрос доказывает своё право на данные сам.
 * В query-строку её класть нельзя — адреса оседают в логах прокси.
 */
export async function api(path, { params = {}, method = 'GET', body = null } = {}) {
  const url = new URL(path, location.href);
  // Чат подставляем всюду сами: без него сервер не поймёт, чьи данные отдавать.
  const query = { chat: state.chat, ...params };
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== '') {
      url.searchParams.set(key, value);
    }
  }

  const options = {
    method,
    headers: { Authorization: `tma ${tg?.initData ?? ''}` },
  };
  if (body !== null) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  const response = await fetch(url, options);

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // Сервер ответил не JSON — покажем хотя бы код.
  }
  if (!response.ok) throw new Error(errorText(payload, response.status));
  return payload;
}

/**
 * Достать человеческий текст ошибки.
 *
 * FastAPI отвечает по-разному: наши ошибки — {error}, HTTPException — {detail}
 * строкой, а провал проверки типов — {detail: [{loc, msg}, …]}.
 */
function errorText(payload, status) {
  if (payload?.error) return payload.error;
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0];
    const field = Array.isArray(first.loc) ? first.loc.at(-1) : '';
    return field ? `Поле «${field}»: ${first.msg}` : first.msg;
  }
  return `Сервер ответил ${status}`;
}
