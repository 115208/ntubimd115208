document.addEventListener('DOMContentLoaded', () => {
    const monthPickerModal = document.getElementById('month-picker-modal');
    const monthPickerBackdrop = document.getElementById('month-picker-backdrop');
    const monthPickerContent = document.getElementById('month-picker-content');
    const monthPickerYearDisplay = document.getElementById('month-picker-year-display');
    const monthPickerGrid = document.getElementById('month-picker-grid');
    const prevYearBtn = document.getElementById('month-picker-prev-year');
    const nextYearBtn = document.getElementById('month-picker-next-year');
    const customDatePickerBtn = document.getElementById('custom-date-picker-btn');
    const monthPrevBtn = document.getElementById('month-prev-btn');
    const monthTodayBtn = document.getElementById('month-today-btn');
    const monthNextBtn = document.getElementById('month-next-btn');
 
    if (!monthPickerModal || !customDatePickerBtn || !monthPickerContent || !monthPickerGrid || !monthPickerYearDisplay || !prevYearBtn || !nextYearBtn || !monthPickerBackdrop) {
        return;
    }

    // 切換月份／日期時必須帶上目前選到的胎數或寶寶，
    // 否則跳轉後 active selection 會被 fallback 蓋掉，畫面跳回別的個案。
    function buildDateUrl(dateIso) {
        const current = new URLSearchParams(window.location.search);
        const params = new URLSearchParams({ date: dateIso });
        const caseId = current.get('case_id');
        const babyId = current.get('baby_id');
        if (caseId) params.set('case_id', caseId);
        if (babyId) params.set('baby_id', babyId);
        return `?${params.toString()}`;
    }

    let currentSelectedYear = new Date().getFullYear();
    let currentSelectedMonth = new Date().getMonth() + 1;
    const initialText = (document.getElementById('custom-date-picker-text')||{}).innerText || '';
    const match = initialText.match(/(\d{4})年\s*(\d+)月/);
 
    if (match) {
        currentSelectedYear = parseInt(match[1], 10);
        currentSelectedMonth = parseInt(match[2], 10);
    }
 
    let tempYear = currentSelectedYear;
    let tempMonth = currentSelectedMonth;
    let pickerMode = 'month';
 
    function renderMonthGrid() {
        monthPickerGrid.innerHTML = '';
 
        for (let month = 1; month <= 12; month++) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'py-4 rounded-full text-base font-bold transition-all cursor-pointer border';
 
            if (tempYear === currentSelectedYear && month === tempMonth) {
                button.className += ' bg-primary text-white shadow-md border-primary';
            } else if (tempMonth === month) {
                button.className += ' bg-primary/10 text-primary border-primary/20';
            } else {
                button.className += ' bg-surface-container-lowest text-on-surface hover:bg-surface-container-low border-surface-container-high';
            }
 
            button.innerText = `${month}月`;
            button.onclick = () => {
                // navigate to selected month (first day)
                const target = `${tempYear}-${String(month).padStart(2,'0')}-01`;
                window.location.href = buildDateUrl(target);
            };
 
            monthPickerGrid.appendChild(button);
        }
    }
 
    function renderYearGrid() {
        monthPickerGrid.innerHTML = '';
 
        const startYear = tempYear - 5;
        for (let offset = 0; offset < 12; offset++) {
            const year = startYear + offset;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'py-4 rounded-full text-base font-bold transition-all cursor-pointer border';
 
            if (year === currentSelectedYear) {
                button.className += ' bg-primary text-white shadow-md border-primary';
            } else if (year === tempYear) {
                button.className += ' bg-primary/10 text-primary border-primary/20';
            } else {
                button.className += ' bg-surface-container-lowest text-on-surface hover:bg-surface-container-low border-surface-container-high';
            }
 
            button.innerText = `${year}年`;
            button.onclick = () => {
                tempYear = year;
                currentSelectedYear = year;
                pickerMode = 'month';
                monthPickerYearDisplay.innerText = `${tempYear}年`;
                renderMonthGrid();
            };
 
            monthPickerGrid.appendChild(button);
        }
    }
 
    function renderPickerGrid() {
        if (pickerMode === 'year') {
            renderYearGrid();
        } else {
            renderMonthGrid();
        }
    }
 
    function goToMonth(year, month) {
        const safeYear = Number(year);
        const safeMonth = Number(month);
        if (!safeYear || !safeMonth) {
            return;
        }
        const target = `${safeYear}-${String(safeMonth).padStart(2,'0')}-01`;
        window.location.href = buildDateUrl(target);
    }
 
    function shiftMonth(delta) {
        let year = currentSelectedYear;
        let month = currentSelectedMonth + delta;
        if (month < 1) {
            month = 12;
            year -= 1;
        } else if (month > 12) {
            month = 1;
            year += 1;
        }
        goToMonth(year, month);
    }
 
    function openMonthPicker() {
        tempYear = currentSelectedYear;
        tempMonth = currentSelectedMonth;
        pickerMode = 'month';
        monthPickerYearDisplay.innerText = `${tempYear}年`;
        renderPickerGrid();
 
        monthPickerModal.classList.remove('opacity-0', 'pointer-events-none');
        monthPickerModal.classList.add('opacity-100', 'pointer-events-auto');
 
        setTimeout(() => {
            monthPickerContent.classList.remove('translate-y-full', 'sm:translate-y-4', 'sm:scale-95');
            monthPickerContent.classList.add('translate-y-0', 'sm:translate-y-0', 'sm:scale-100');
        }, 10);
    }
 
    function closeMonthPickerModal() {
        monthPickerContent.classList.remove('translate-y-0', 'sm:translate-y-0', 'sm:scale-100');
        monthPickerContent.classList.add('translate-y-full', 'sm:translate-y-4', 'sm:scale-95');
 
        setTimeout(() => {
            monthPickerModal.classList.remove('opacity-100', 'pointer-events-auto');
            monthPickerModal.classList.add('opacity-0', 'pointer-events-none');
        }, 300);
    }
 
    customDatePickerBtn.addEventListener('click', openMonthPicker);
    monthPickerBackdrop.addEventListener('click', closeMonthPickerModal);
    if (monthPrevBtn) {
        monthPrevBtn.addEventListener('click', () => shiftMonth(-1));
    }
    if (monthNextBtn) {
        monthNextBtn.addEventListener('click', () => shiftMonth(1));
    }
    if (monthTodayBtn) {
        monthTodayBtn.addEventListener('click', () => {
            const current = new URLSearchParams(window.location.search);
            const params = new URLSearchParams();
            const caseId = current.get('case_id');
            const babyId = current.get('baby_id');
            if (caseId) params.set('case_id', caseId);
            if (babyId) params.set('baby_id', babyId);
            const qs = params.toString();
            window.location.href = qs ? `?${qs}` : window.location.pathname;
        });
    }
 
    prevYearBtn.addEventListener('click', () => {
        tempYear -= pickerMode === 'year' ? 12 : 1;
        monthPickerYearDisplay.innerText = `${tempYear}年`;
        renderPickerGrid();
    });
 
    nextYearBtn.addEventListener('click', () => {
        tempYear += pickerMode === 'year' ? 12 : 1;
        monthPickerYearDisplay.innerText = `${tempYear}年`;
        renderPickerGrid();
    });
 
    monthPickerYearDisplay.addEventListener('click', () => {
        pickerMode = pickerMode === 'year' ? 'month' : 'year';
        monthPickerYearDisplay.innerText = `${tempYear}年`;
        renderPickerGrid();
    });
});
 
