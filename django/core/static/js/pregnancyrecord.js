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
 
    // 切換月份／日期時必須帶上目前選到的胎數或寶寶，
    // 否則跳轉後 active selection 會被 fallback 蓋掉，畫面跳回別的個案。
    // 參數名稱不寫死：優先沿用伺服器產生的日曆格子連結（內含 active_selection_query），
    // 再補上網址列上其他的參數，唯獨 date 一律由這裡決定。
    function baseParams() {
        const params = new URLSearchParams();
        const cellLink = document.querySelector('a[href^="?date="]');
        if (cellLink) {
            new URLSearchParams(cellLink.getAttribute('href').replace(/^\?/, '')).forEach((value, key) => {
                if (key !== 'date') params.set(key, value);
            });
        }
        new URLSearchParams(window.location.search).forEach((value, key) => {
            if (key !== 'date' && !params.has(key)) params.set(key, value);
        });
        return params;
    }

    function buildDateUrl(dateIso) {
        const params = baseParams();
        params.set('date', dateIso);
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
        if (!monthPickerGrid) {
            return;
        }
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
        if (!monthPickerGrid) {
            return;
        }
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
        if (!monthPickerModal || !monthPickerContent || !monthPickerYearDisplay) {
            return;
        }
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
        if (!monthPickerModal || !monthPickerContent) {
            return;
        }
        monthPickerContent.classList.remove('translate-y-0', 'sm:translate-y-0', 'sm:scale-100');
        monthPickerContent.classList.add('translate-y-full', 'sm:translate-y-4', 'sm:scale-95');
 
        setTimeout(() => {
            monthPickerModal.classList.remove('opacity-100', 'pointer-events-auto');
            monthPickerModal.classList.add('opacity-0', 'pointer-events-none');
        }, 300);
    }
 
    if (customDatePickerBtn) {
        customDatePickerBtn.addEventListener('click', openMonthPicker);
    }
    if (monthPickerBackdrop) {
        monthPickerBackdrop.addEventListener('click', closeMonthPickerModal);
    }
    if (monthPrevBtn) {
        monthPrevBtn.addEventListener('click', () => shiftMonth(-1));
    }
    if (monthNextBtn) {
        monthNextBtn.addEventListener('click', () => shiftMonth(1));
    }
    if (monthTodayBtn) {
        monthTodayBtn.addEventListener('click', () => {
            const qs = baseParams().toString();
            window.location.href = qs ? `?${qs}` : window.location.pathname;
        });
    }
 
    if (prevYearBtn) {
        prevYearBtn.addEventListener('click', () => {
            tempYear -= pickerMode === 'year' ? 12 : 1;
            if (monthPickerYearDisplay) {
                monthPickerYearDisplay.innerText = `${tempYear}年`;
            }
            renderPickerGrid();
        });
    }

    if (nextYearBtn) {
        nextYearBtn.addEventListener('click', () => {
            tempYear += pickerMode === 'year' ? 12 : 1;
            if (monthPickerYearDisplay) {
                monthPickerYearDisplay.innerText = `${tempYear}年`;
            }
            renderPickerGrid();
        });
    }

    if (monthPickerYearDisplay) {
        monthPickerYearDisplay.addEventListener('click', () => {
            pickerMode = pickerMode === 'year' ? 'month' : 'year';
            monthPickerYearDisplay.innerText = `${tempYear}年`;
            renderPickerGrid();
        });
    }
});