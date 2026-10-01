document.addEventListener('DOMContentLoaded', () => {
    const attachedInputs = new WeakSet();
    const overlay = document.createElement('div');
    overlay.className = 'date-picker-overlay';
    overlay.hidden = true;
    overlay.innerHTML = `
        <section class="date-picker-dialog" role="dialog" aria-modal="true" aria-label="選擇日期">
            <header class="date-picker-header">
                <button type="button" class="date-picker-nav" data-shift="-1" aria-label="上一年"><span class="material-symbols-outlined">chevron_left</span></button>
                <button type="button" class="date-picker-title" data-action="months"></button>
                <button type="button" class="date-picker-nav" data-shift="1" aria-label="下一年"><span class="material-symbols-outlined">chevron_right</span></button>
            </header>
            <div class="date-picker-months"></div>
            <div class="date-picker-calendar" hidden>
                <div class="date-picker-weekdays"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
                <div class="date-picker-days"></div>
            </div>
            <div class="date-picker-time" hidden><label>時間</label><select data-time-hour aria-label="小時"></select><span>:</span><select data-time-minute aria-label="分鐘"></select></div>
            <footer class="date-picker-actions">
                <button type="button" class="date-picker-action" data-action="cancel">取消</button>
                <button type="button" class="date-picker-action" data-action="today">今天</button>
            </footer>
        </section>`;
    document.body.appendChild(overlay);

    const title = overlay.querySelector('.date-picker-title');
    const months = overlay.querySelector('.date-picker-months');
    const calendar = overlay.querySelector('.date-picker-calendar');
    const days = overlay.querySelector('.date-picker-days');
    const timeControls = overlay.querySelector('.date-picker-time');
    const hourSelect = overlay.querySelector('[data-time-hour]');
    const minuteSelect = overlay.querySelector('[data-time-minute]');
    const todayButton = overlay.querySelector('[data-action="today"]');
    for (let hour = 0; hour < 24; hour++) hourSelect.add(new Option(String(hour).padStart(2, '0'), String(hour).padStart(2, '0')));
    for (let minute = 0; minute < 60; minute++) minuteSelect.add(new Option(String(minute).padStart(2, '0'), String(minute).padStart(2, '0')));

    let activeInput = null;
    let viewYear = 0;
    let viewMonth = 0;
    const isDateTime = () => activeInput?.dataset.datePickerType === 'datetime-local';
    const isTimeOnly = () => activeInput?.dataset.datePickerType === 'time';
    const selectedTime = () => `${hourSelect.value || '09'}:${minuteSelect.value || '00'}`;
    const isoDate = (year, month, day) => `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const currentDate = () => {
        if (activeInput?.value) {
            const [year, month, day] = activeInput.value.split('T')[0].split('-').map(Number);
            if (year && month && day) return new Date(year, month - 1, day);
        }
        return new Date();
    };
    const boundDate = (value) => value ? new Date(`${value.slice(0, 10)}T00:00:00`) : null;
    const updateInput = (value) => {
        activeInput.value = value;
        activeInput.dispatchEvent(new Event('input', { bubbles: true }));
        activeInput.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const close = () => { overlay.hidden = true; activeInput = null; };
    const renderMonths = () => {
        title.textContent = `${viewYear}年`;
        title.hidden = false;
        overlay.querySelectorAll('[data-shift]').forEach((button) => { button.hidden = false; });
        months.hidden = false;
        calendar.hidden = true;
        timeControls.hidden = true;
        months.replaceChildren();
        for (let month = 0; month < 12; month++) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'date-picker-month';
            button.textContent = `${month + 1}月`;
            button.setAttribute('aria-pressed', String(viewYear === currentDate().getFullYear() && month === currentDate().getMonth()));
            button.addEventListener('click', () => { viewMonth = month; renderDays(); });
            months.appendChild(button);
        }
    };
    const renderDays = () => {
        title.textContent = `${viewYear}年${viewMonth + 1}月`;
        months.hidden = true;
        calendar.hidden = false;
        timeControls.hidden = !isDateTime();
        days.replaceChildren();
        const firstWeekday = new Date(viewYear, viewMonth, 1).getDay();
        const count = new Date(viewYear, viewMonth + 1, 0).getDate();
        for (let index = 0; index < firstWeekday; index++) {
            const blank = document.createElement('span');
            blank.className = 'date-picker-day is-outside';
            days.appendChild(blank);
        }
        const minValue = activeInput.dataset.datePickerMin || activeInput.getAttribute('min') || '';
        const maxValue = activeInput.dataset.datePickerMax || activeInput.getAttribute('max') || '';
        const min = boundDate(minValue);
        const max = boundDate(maxValue);
        for (let day = 1; day <= count; day++) {
            const date = new Date(viewYear, viewMonth, day);
            const iso = isoDate(viewYear, viewMonth, day);
            const candidate = isDateTime() ? `${iso}T${selectedTime()}` : iso;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'date-picker-day';
            button.textContent = day;
            button.setAttribute('aria-pressed', String(candidate === activeInput.value));
            button.disabled = (min && date < min) || (max && date > max)
                || (minValue && candidate < minValue)
                || (maxValue && candidate > maxValue);
            button.addEventListener('click', () => {
                updateInput(isDateTime() ? `${iso}T${selectedTime()}` : iso);
                close();
            });
            days.appendChild(button);
        }
    };
    const open = (input) => {
        activeInput = input;
        if (isDateTime() || isTimeOnly()) {
            const currentTime = isTimeOnly() ? input.value : input.value.split('T')[1];
            const [hour = '09', minute = '00'] = (currentTime || '09:00').split(':');
            hourSelect.value = hour;
            minuteSelect.value = minute;
        }
        if (isTimeOnly()) {
            title.textContent = '選擇時間';
            todayButton.textContent = '現在';
            overlay.querySelectorAll('[data-shift]').forEach((button) => { button.hidden = true; });
            months.hidden = true;
            calendar.hidden = true;
            timeControls.hidden = false;
            overlay.hidden = false;
            return;
        }
        const initial = currentDate();
        todayButton.textContent = '今天';
        viewYear = initial.getFullYear();
        viewMonth = initial.getMonth();
        renderMonths();
        overlay.hidden = false;
    };

    overlay.addEventListener('click', (event) => {
        if (event.target === overlay || event.target.closest('[data-action="cancel"]')) { close(); return; }
        if (event.target.closest('[data-action="today"]')) {
            const now = new Date();
            if (isTimeOnly()) {
                updateInput(`${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`);
                close();
                return;
            }
            const today = isoDate(now.getFullYear(), now.getMonth(), now.getDate());
            const candidate = isDateTime() ? `${today}T${selectedTime()}` : today;
            const min = activeInput.dataset.datePickerMin || activeInput.getAttribute('min') || '';
            const max = activeInput.dataset.datePickerMax || activeInput.getAttribute('max') || '';
            if ((!min || candidate >= min) && (!max || candidate <= max)) updateInput(candidate);
            close();
            return;
        }
        const shift = event.target.closest('[data-shift]');
        if (shift) { viewYear += Number(shift.dataset.shift); renderMonths(); return; }
        if (event.target.closest('[data-action="months"]') && months.hidden) renderMonths();
    });
    const handleTimeChange = () => {
        if (isTimeOnly()) {
            const value = selectedTime();
            const min = activeInput.dataset.datePickerMin || activeInput.getAttribute('min') || '';
            const max = activeInput.dataset.datePickerMax || activeInput.getAttribute('max') || '';
            if ((!min || value >= min) && (!max || value <= max)) updateInput(value);
        } else if (!months.hidden) renderDays();
    };
    hourSelect.addEventListener('change', handleTimeChange);
    minuteSelect.addEventListener('change', handleTimeChange);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !overlay.hidden) close(); });

    const attachInput = (input) => {
        if (attachedInputs.has(input) || input.disabled || input.readOnly) return;
        attachedInputs.add(input);
        input.dataset.datePickerType = input.type;
        input.dataset.datePickerMin = input.getAttribute('min') || '';
        input.dataset.datePickerMax = input.getAttribute('max') || '';
        input.type = 'text';
        input.inputMode = 'none';
        input.readOnly = true;
        input.classList.add('date-picker-input');
        input.setAttribute('aria-haspopup', 'dialog');
        input.setAttribute('autocomplete', 'off');
        input.addEventListener('click', (event) => { event.preventDefault(); open(input); });
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(input); }
        });
        const wrapper = input.closest('[data-date-picker-trigger]');
        if (wrapper) wrapper.addEventListener('click', (event) => { if (event.target !== input) open(input); });
        if (input.required && input.form) {
            input.form.addEventListener('submit', (event) => {
                if (input.value) return;
                event.preventDefault();
                open(input);
            });
        }
    };
    const observeInputs = (root) => {
        if (!(root instanceof Element)) return;
        if (root.matches('input[type="date"], input[type="datetime-local"], input[type="time"]')) attachInput(root);
        root.querySelectorAll('input[type="date"], input[type="datetime-local"], input[type="time"]').forEach(attachInput);
    };
    document.querySelectorAll('input[type="date"], input[type="datetime-local"], input[type="time"]').forEach(attachInput);
    new MutationObserver((mutations) => mutations.forEach((mutation) => mutation.addedNodes.forEach(observeInputs)))
        .observe(document.body, { childList: true, subtree: true });
});
