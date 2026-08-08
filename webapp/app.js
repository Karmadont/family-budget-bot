/*
  app.js — запуск приложения и переключение экранов.

  Про Telegram нужно понимать одну вещь: Telegram.WebApp.initData — строка с
  данными пользователя и подписью, её мы отправляем на сервер при каждом
  запросе. Рядом лежит initDataUnsafe — то же самое, разобранное, но БЕЗ
  проверки подписи. Подставить туда чужой id может кто угодно, поэтому решения
  по нему не принимаются никогда: только отрисовка.
*/

import * as overview from './overview.js';
import * as spending from './spending.js';
import { api, state, tg } from './store.js';

const el = (id) => document.getElementById(id);

const SCREENS = {
  overview: { section: 'screen-overview', enter: () => overview.load() },
  spending: { section: 'screen-spending', enter: () => spending.show() },
};

let current = 'overview';

function showScreen(name) {
  if (!SCREENS[name]) return;
  current = name;
  for (const [key, screen] of Object.entries(SCREENS)) {
    el(screen.section).hidden = key !== name;
  }
  for (const button of el('tabs').children) {
    button.setAttribute('aria-selected', String(button.dataset.screen === name));
  }
  SCREENS[name].enter().catch(showError);
}

function showError(error) {
  el('loading').hidden = true;
  el('app').hidden = true;
  el('error').hidden = false;
  el('error-text').textContent = error?.message || String(error);
}

function updateReviewBadge(count) {
  const badge = el('review-badge');
  badge.textContent = count > 99 ? '99+' : String(count);
  badge.hidden = !count;
}

async function boot() {
  if (!tg) {
    showError(new Error('Это приложение открывается внутри Telegram.'));
    return;
  }

  tg.ready();
  tg.expand();
  applyTheme();
  tg.onEvent('themeChanged', applyTheme);

  if (!tg.initData) {
    showError(new Error(
      'Telegram не передал данные авторизации. Откройте приложение кнопкой из чата.'));
    return;
  }

  // Чат приходит в ссылке (?startapp=…) и попадает сюда как start_param.
  state.chat = tg.initDataUnsafe?.start_param ?? null;

  try {
    const me = await api('/api/me', { params: { chat: '' } });
    state.currency = me.currency || state.currency;
    state.categories = me.categories || [];
    state.user = me.user;

    if (!me.chats.length) {
      showError(new Error(
        'Не нашёл ни одного чата с вашими покупками. Добавьте бота в чат и напишите, что купили.'));
      return;
    }
    if (!state.chat) state.chat = me.chats[0].token;
    setupChatPicker(me.chats);

    overview.mount();
    spending.mount({ onReview: updateReviewBadge });

    el('tabs').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-screen]');
      if (button && button.dataset.screen !== current) showScreen(button.dataset.screen);
    });

    await overview.load();
    updateReviewBadge(state.review);
    el('loading').hidden = true;
    el('error').hidden = true;
    el('app').hidden = false;
  } catch (error) {
    showError(error);
  }
}

function setupChatPicker(chats) {
  if (chats.length < 2) return;
  const picker = el('chat-picker');
  picker.innerHTML = chats
    .map((chat) => `<option value="${chat.token}">${escapeAttr(chat.title)}</option>`)
    .join('');
  picker.value = chats.find((chat) => chat.token === state.chat)?.token ?? chats[0].token;
  state.chat = picker.value;
  picker.hidden = false;
  picker.addEventListener('change', async () => {
    state.chat = picker.value;
    // Обе ленты относились к прежнему чату — перезагружаем ту, что на экране.
    await spending.reload().catch(() => {});
    showScreen(current);
  });
}

function escapeAttr(text) {
  const node = document.createElement('span');
  node.textContent = text ?? '';
  return node.innerHTML;
}

function applyTheme() {
  document.documentElement.dataset.theme = tg.colorScheme === 'dark' ? 'dark' : 'light';
  try {
    tg.setHeaderColor('bg_color');
  } catch {
    // Старые клиенты этого не умеют — шапка просто останется своей.
  }
}

el('retry').addEventListener('click', boot);

boot();
