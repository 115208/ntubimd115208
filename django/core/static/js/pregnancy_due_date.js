/**
 * EDD from LMP (first day of last menstrual period).
 * Uses Naegele's rule (month −3, day +7, year +1), equivalent to LMP + 280 days.
 */
(function () {
    function parseLocalDate(ymd) {
        const parts = ymd.split('-').map(Number);
        if (parts.length !== 3 || parts.some(Number.isNaN)) {
            return null;
        }
        return new Date(parts[0], parts[1] - 1, parts[2]);
    }

    function formatLocalDate(date) {
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, '0');
        const d = String(date.getDate()).padStart(2, '0');
        return `${y}-${m}-${d}`;
    }

    function daysInMonth(year, month) { // month: 1-12
        return new Date(year, month, 0).getDate();
    }

    /**
     * Naegele's rule：年 +1、月 −3、日 +7。
     * 月份先位移再「夾」到該月最後一天，才會與後端 _calculate_expected_date 一致。
     * 舊版直接 setMonth(-3) 會讓 5/31 溢位成 3/3（多算 3 天）。
     */
    function dueDateFromLMP(ymd) {
        const lmp = parseLocalDate(ymd);
        if (!lmp) {
            return '';
        }
        let year = lmp.getFullYear() + 1;
        let month = lmp.getMonth() + 1 - 3; // 1-12
        if (month <= 0) {
            month += 12;
            year -= 1;
        }
        const day = Math.min(lmp.getDate(), daysInMonth(year, month));
        const due = new Date(year, month - 1, day);
        due.setDate(due.getDate() + 7);
        return formatLocalDate(due);
    }

    function bindDueDateAutoCalc(menstruationInput, expecteddateInput, options) {
        if (!menstruationInput || !expecteddateInput) {
            return;
        }
        const forceSync = options && options.forceSync;

        function syncExpectedDate() {
            const lmp = menstruationInput.value;
            if (!lmp) {
                return;
            }
            // 預設：只在預產期還沒有值時自動帶入，不覆寫使用者（或醫生）已填的預產期。
            // forceSync=true（編輯頁）：每次改 LMP 都重算，讓編輯結果即時反映。
            if (!forceSync && expecteddateInput.value) {
                return;
            }
            const calculated = dueDateFromLMP(lmp);
            if (calculated) {
                expecteddateInput.value = calculated;
            }
        }

        menstruationInput.addEventListener('input', syncExpectedDate);
        menstruationInput.addEventListener('change', syncExpectedDate);
    }

    function bindPregnancyDatePair(form, menstruationInput, expecteddateInput, options) {
        bindDueDateAutoCalc(menstruationInput, expecteddateInput, options);
        if (!form || !menstruationInput || !expecteddateInput) {
            return;
        }

        function validateDatePair() {
            const hasAnyDate = Boolean(menstruationInput.value || expecteddateInput.value);
            expecteddateInput.setCustomValidity(hasAnyDate ? '' : '請填寫最後月經日期或預產期其中一個');
            return hasAnyDate;
        }

        menstruationInput.addEventListener('input', validateDatePair);
        expecteddateInput.addEventListener('input', validateDatePair);
        form.addEventListener('submit', function (event) {
            if (!validateDatePair()) {
                event.preventDefault();
                expecteddateInput.reportValidity();
            }
        });
    }

    window.PregnancyDueDate = { dueDateFromLMP, bindDueDateAutoCalc, bindPregnancyDatePair };
})();
