'use strict';
const $ = id => document.getElementById(id);
let state = null;
let applied = new URLSearchParams();
let offset = 0;
let busy = false;
let loading = false;
const duration = seconds => {
  const n = Math.max(0, Math.floor(seconds));
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map(v => String(v).padStart(2, '0')).join(':');
};
const stamp = seconds => new Date(seconds * 1000).toISOString().slice(0, 16).replace('T', ' ');
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').className = error ? 'error' : '';
  $('notice').hidden = !message;
}
async function request(url, data) {
  const response = await fetch(url, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Ошибка сервера.');
  return result;
}
function options(select, employees, placeholder) {
  const selected = select.value;
  select.replaceChildren(new Option(placeholder, ''));
  employees.forEach(e => select.add(new Option(e.name, e.id)));
  if (employees.some(e => String(e.id) === selected)) select.value = selected;
}
function render() {
  options($('timer-employee'), state.employees, 'Выберите сотрудника');
  options($('filter-employee'), state.employees, 'Вся команда');
  if (!state.employees.length) $('employees').replaceChildren(element('p', 'Добавьте первого участника команды.', 'hint'));
  else $('employees').replaceChildren(...state.employees.map(e => {
    const row = element('div', undefined, 'employee');
    row.append(element('span', e.name.slice(0, 2).toUpperCase(), 'avatar'), element('span', e.name));
    if (state.running.some(r => r.employee_id === e.id)) row.append(element('small', 'В РАБОТЕ'));
    return row;
  }));
  $('running-count').textContent = state.running.length;
  $('running').replaceChildren(...state.running.map(entry => {
    const row = element('div', undefined, 'active-row');
    const info = element('div', undefined, 'active-info');
    info.append(element('strong', entry.employee_name), element('span', entry.description));
    const elapsed = element('div', '', 'elapsed');
    elapsed.dataset.started = entry.started_at;
    const stop = element('button', '■ Остановить', 'stop');
    stop.type = 'button';
    stop.setAttribute('aria-label', `Остановить таймер: ${entry.employee_name}`);
    stop.addEventListener('click', () => mutate(stop, async () => {
      await request('/api/timers/stop', {entry_id: entry.id});
      notice('Таймер остановлен. Время сохранено.');
    }));
    row.append(element('span', entry.employee_name.slice(0, 2).toUpperCase(), 'avatar'), info, elapsed, stop);
    return row;
  }));
  if (!state.running.length) $('running').append(element('p', 'Все таймеры на паузе. Самое время начать новую задачу.', 'empty'));
  $('total').textContent = duration(state.total_seconds);
  $('entry-count').textContent = state.entries.length;
  $('summary').replaceChildren(...state.summary.map(e => {
    const node = element('div', e.name, 'summary-item');
    node.append(element('b', duration(e.seconds)));
    return node;
  }));
  $('entries').replaceChildren(...state.entries.map(entry => {
    const tr = element('tr');
    const task = element('td');
    task.append(element('strong', entry.employee_name), element('span', entry.description));
    const status = element('td');
    status.append(element('span', entry.stopped_at === null ? 'В работе' : 'Завершено', entry.stopped_at === null ? 'state' : 'state done'));
    tr.append(task, element('td', stamp(entry.started_at)), element('td', duration(entry.period_seconds), 'duration'), status);
    return tr;
  }));
  $('empty-history').hidden = state.entries.length > 0;
  $('export').href = '/api/export.csv?' + applied;
  tick();
}
function tick() {
  const now = Date.now() / 1000 + offset;
  document.querySelectorAll('[data-started]').forEach(node => {
    node.textContent = duration(now - Number(node.dataset.started));
  });
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const next = await request('/api/state?' + applied);
    offset = next.server_time - Date.now() / 1000;
    state = next;
    render();
  } finally { loading = false; }
}
async function mutate(button, action) {
  if (busy) return;
  busy = true;
  button.disabled = true;
  try { await action(); await refresh(); }
  catch (error) { notice(error.message, true); }
  finally { busy = false; button.disabled = false; }
}
$('employee-form').addEventListener('submit', event => {
  event.preventDefault();
  mutate(event.submitter, async () => {
    const result = await request('/api/employees', {name: $('employee-name').value});
    $('employee-name').value = '';
    // Add the option before refresh so the selected employee survives rendering.
    $('timer-employee').add(new Option(result.name, result.id));
    $('timer-employee').value = result.id;
    notice('Сотрудник добавлен. Можно запускать таймер.');
  });
});
$('timer-form').addEventListener('submit', event => {
  event.preventDefault();
  mutate(event.submitter, async () => {
    await request('/api/timers/start', {employee_id: Number($('timer-employee').value), description: $('description').value});
    $('description').value = '';
    notice('Таймер запущен. Можно закрыть страницу — отсчёт продолжится.');
  });
});
$('filters').addEventListener('submit', async event => {
  event.preventDefault();
  const previous = applied;
  applied = new URLSearchParams();
  [['employee_id', 'filter-employee'], ['from', 'from'], ['to', 'to']].forEach(([key, id]) => {
    if ($(id).value) applied.set(key, $(id).value);
  });
  try { await refresh(); notice('Отчёт обновлён.'); }
  catch (error) { applied = previous; notice(error.message, true); }
});
$('reset').addEventListener('click', async () => {
  $('filters').reset(); applied = new URLSearchParams();
  try { await refresh(); notice('Показаны все записи.'); }
  catch (error) { notice(error.message, true); }
});
refresh().catch(() => notice('Не удалось загрузить данные. Проверьте, запущен ли сервер, и обновите страницу.', true));
setInterval(tick, 1000);
setInterval(() => { if (!busy) refresh().catch(() => notice('Нет связи с сервером. Повторяем подключение; сохранённые данные не потеряны.', true)); }, 10000);
