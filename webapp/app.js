const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
}

function initData() {
  return tg ? tg.initData : "";
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": initData(),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `Ошибка ${res.status}`);
  }
  return data;
}

function fmt(n) {
  return Math.round(n).toLocaleString("ru-RU");
}

function showToast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 2500);
}

// ---------- Навигация ----------

const screens = ["home", "expenses", "shopping"];
const titles = {
  home: "Главная",
  expenses: "Расходы",
  shopping: "Список покупок",
};

function showScreen(name) {
  screens.forEach((s) => {
    document.getElementById(`screen-${s}`).classList.toggle("hidden", s !== name);
  });
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.screen === name);
  });
  document.getElementById("screen-title").textContent = titles[name];
  if (name === "home") loadDashboard();
  if (name === "expenses") loadExpenses();
  if (name === "shopping") loadShopping();
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => showScreen(btn.dataset.screen));
});

// ---------- Главная ----------

async function loadDashboard() {
  try {
    const d = await api("/api/dashboard");

    document.getElementById("shared-remaining").textContent = fmt(d.shared_remaining);
    document.getElementById("shared-breakdown").textContent =
      `Бюджет ${fmt(d.shared_budget_total)} − потрачено ${fmt(d.shared_spent)} (с ${d.period_start})`;

    const perPerson = document.getElementById("per-person-list");
    perPerson.innerHTML = "";
    d.per_person.forEach((p) => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `
        <div class="desc">
          <span>${p.display_name}</span>
          <span class="cat">бюджет ${fmt(p.budget)} · потрачено ${fmt(p.spent)}</span>
        </div>
        <span class="amount">${fmt(p.remaining)}</span>`;
      perPerson.appendChild(row);
    });

    if (d.personal_budget === null) {
      document.getElementById("personal-remaining").textContent = "не задан";
      document.getElementById("personal-breakdown").textContent = "Задайте сумму ниже";
    } else {
      document.getElementById("personal-remaining").textContent = fmt(d.personal_remaining);
      document.getElementById("personal-breakdown").textContent =
        `Бюджет ${fmt(d.personal_budget)} − потрачено ${fmt(d.personal_spent)}`;
    }

    document.getElementById("today-total").textContent = fmt(d.today_total);

    await loadPeriodSettingsIfAdmin();
  } catch (e) {
    showToast(e.message);
  }
}

async function loadPeriodSettingsIfAdmin() {
  const card = document.getElementById("period-settings-card");
  try {
    const me = await api("/api/me");
    if (!me.is_admin) {
      card.classList.add("hidden");
      return;
    }
    const period = await api("/api/period");
    card.classList.remove("hidden");
    document.getElementById("period-current").textContent =
      `Сейчас: ${period.day}-е число каждого месяца (текущий период с ${period.period_start})`;
    document.getElementById("period-input").value = period.day;
  } catch (e) {
    card.classList.add("hidden");
  }
}

document.getElementById("period-submit").addEventListener("click", async () => {
  const input = document.getElementById("period-input");
  const hint = document.getElementById("period-hint");
  hint.textContent = "";
  hint.classList.remove("error");
  const day = parseInt(input.value, 10);
  if (!day) return;
  try {
    const res = await api("/api/period", { method: "POST", body: JSON.stringify({ day }) });
    hint.textContent = `Сохранено: ${res.day}-е число, текущий период с ${res.period_start}`;
    loadDashboard();
  } catch (e) {
    hint.textContent = e.message;
    hint.classList.add("error");
  }
});

document.getElementById("budget-submit").addEventListener("click", async () => {
  const input = document.getElementById("budget-input");
  const hint = document.getElementById("budget-hint");
  hint.textContent = "";
  hint.classList.remove("error");
  const raw = input.value.trim();
  if (!raw) return;
  try {
    const res = await api("/api/budget", { method: "POST", body: JSON.stringify({ amount: raw }) });
    hint.textContent = `Личный бюджет: ${fmt(res.amount)}`;
    input.value = "";
    loadDashboard();
  } catch (e) {
    hint.textContent = e.message;
    hint.classList.add("error");
  }
});

// ---------- Расходы ----------

const expenseScopeSelect = document.getElementById("expenses-scope-select");
const expenseModeSelect = document.getElementById("expenses-mode-select");

expenseScopeSelect.addEventListener("change", loadExpenses);
expenseModeSelect.addEventListener("change", loadExpenses);

function groupExpensesByCategory(items) {
  const groups = {};
  items.forEach((item) => {
    const cat = item.category || "Другое";
    if (!groups[cat]) groups[cat] = { category: cat, amount: 0, items: [] };
    groups[cat].amount += item.amount;
    groups[cat].items.push(item);
  });
  return Object.values(groups).sort((a, b) => b.amount - a.amount);
}

function renderExpensesDetailed(container, items) {
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-row";
    const d = new Date(item.created_at);
    const dateStr = `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    row.innerHTML = `
      <div class="desc">
        <span>${item.description}</span>
        <span class="cat">${item.category} · ${item.added_by || ""} · ${dateStr}</span>
      </div>
      <span class="amount">${fmt(item.amount)}</span>`;
    container.appendChild(row);
  });
}

async function loadExpenses() {
  const scope = expenseScopeSelect.value;
  const mode = expenseModeSelect.value;
  const list = document.getElementById("expenses-list");
  const totalLine = document.getElementById("expenses-total");
  list.innerHTML = "";
  totalLine.textContent = "";

  try {
    // "За всё время" + "По категориям" — это и есть разбивка по месяцам
    // (отдельным блоком на каждый месяц), а не одна общая сумма.
    if (scope === "all" && mode === "categories") {
      const data = await api("/api/months");
      if (!data.months.length) {
        list.innerHTML = '<div class="empty">Расходов пока нет</div>';
        return;
      }
      let grandTotal = 0;
      data.months.slice().reverse().forEach((m) => {
        grandTotal += m.total;
        const block = document.createElement("div");
        block.className = "month-block";
        block.innerHTML = `<h3>${m.label} — ${fmt(m.total)}</h3>`;
        const monthList = document.createElement("div");
        monthList.className = "list";
        renderTotalsList(monthList, m.totals);
        block.appendChild(monthList);
        list.appendChild(block);
      });
      totalLine.textContent = `Итого: ${fmt(grandTotal)}`;
      return;
    }

    const data = await api(`/api/expenses?scope=${scope}`);
    totalLine.textContent = `Итого: ${fmt(data.total)}`;
    if (!data.items.length) {
      list.innerHTML = '<div class="empty">Расходов пока нет</div>';
      return;
    }

    if (mode === "categories") {
      renderTotalsList(list, groupExpensesByCategory(data.items));
    } else {
      renderExpensesDetailed(list, data.items);
    }
  } catch (e) {
    showToast(e.message);
  }
}

function renderTotalsList(container, totals) {
  container.innerHTML = "";
  if (!totals.length) {
    container.innerHTML = '<div class="empty">Нет данных</div>';
    return;
  }
  totals.forEach((t) => {
    const wrap = document.createElement("div");
    wrap.className = "cat-group";

    const row = document.createElement("div");
    row.className = "list-row cat-row";
    row.innerHTML = `
      <span><span class="arrow">▸</span> ${t.category}</span>
      <span class="amount">${fmt(t.amount)}</span>`;

    const details = document.createElement("div");
    details.className = "cat-details hidden";
    details.innerHTML = (t.items || [])
      .map((i) => {
        const d = new Date(i.created_at);
        const dateStr = `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}`;
        return `
          <div class="list-row cat-item">
            <div class="desc">
              <span>${i.description}</span>
              <span class="cat">${i.added_by || ""} · ${dateStr}</span>
            </div>
            <span class="amount">${fmt(i.amount)}</span>
          </div>`;
      })
      .join("");

    row.addEventListener("click", () => {
      const willShow = details.classList.contains("hidden");
      details.classList.toggle("hidden", !willShow);
      row.querySelector(".arrow").textContent = willShow ? "▾" : "▸";
    });

    wrap.appendChild(row);
    wrap.appendChild(details);
    container.appendChild(wrap);
  });
}

async function loadCategories() {
  try {
    const periodData = await api("/api/categories");
    const periodEl = document.getElementById("categories-period");
    periodEl.innerHTML = `<div class="total-line">Итого: ${fmt(periodData.total)}</div>`;
    const periodList = document.createElement("div");
    periodList.className = "list";
    renderTotalsList(periodList, periodData.totals);
    periodEl.appendChild(periodList);

    const monthsData = await api("/api/months");
    const monthsEl = document.getElementById("categories-months");
    monthsEl.innerHTML = "";
    monthsData.months.slice().reverse().forEach((m) => {
      const block = document.createElement("div");
      block.className = "month-block";
      block.innerHTML = `<h3>${m.label} — ${fmt(m.total)}</h3>`;
      const list = document.createElement("div");
      list.className = "list";
      renderTotalsList(list, m.totals);
      block.appendChild(list);
      monthsEl.appendChild(block);
    });
  } catch (e) {
    showToast(e.message);
  }
}

// ---------- Список покупок ----------

function startEditingShoppingItem(row) {
  const itemId = row.dataset.id;
  const originalName = row.dataset.name;
  row.innerHTML = "";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "edit-input";
  input.value = originalName;

  const saveBtn = document.createElement("button");
  saveBtn.textContent = "✓";
  saveBtn.className = "save-btn";

  const cancelBtn = document.createElement("button");
  cancelBtn.textContent = "✕";
  cancelBtn.className = "del-btn";

  const save = async () => {
    const newName = input.value.trim();
    if (!newName || newName === originalName) {
      loadShopping();
      return;
    }
    try {
      await api("/api/shopping/rename", {
        method: "POST",
        body: JSON.stringify({ id: Number(itemId), name: newName }),
      });
      loadShopping();
    } catch (e) {
      showToast(e.message);
    }
  };

  saveBtn.addEventListener("click", save);
  cancelBtn.addEventListener("click", () => loadShopping());
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") save();
    if (ev.key === "Escape") loadShopping();
  });

  row.append(input, saveBtn, cancelBtn);
  input.focus();
  input.select();
}

async function loadShopping() {
  const list = document.getElementById("shopping-list");
  try {
    const data = await api("/api/shopping");
    list.innerHTML = "";
    if (!data.items.length) {
      list.innerHTML = '<div class="empty">Список покупок пуст</div>';
      return;
    }
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "shopping-item list-row" + (item.done ? " done" : "");
      row.dataset.id = item.id;
      row.dataset.name = item.item;

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = item.done;
      checkbox.addEventListener("change", async () => {
        try {
          await api("/api/shopping/done", {
            method: "POST",
            body: JSON.stringify({ names: [row.dataset.name] }),
          });
          loadShopping();
        } catch (e) {
          showToast(e.message);
        }
      });

      const text = document.createElement("span");
      text.className = "item-text";
      text.textContent = item.item;

      const editBtn = document.createElement("button");
      editBtn.className = "edit-btn";
      editBtn.textContent = "✎";
      editBtn.addEventListener("click", () => startEditingShoppingItem(row));

      const delBtn = document.createElement("button");
      delBtn.className = "del-btn";
      delBtn.textContent = "✕";
      delBtn.addEventListener("click", async () => {
        try {
          await api("/api/shopping/delete", {
            method: "POST",
            body: JSON.stringify({ names: [row.dataset.name] }),
          });
          loadShopping();
        } catch (e) {
          showToast(e.message);
        }
      });

      row.append(checkbox, text, editBtn, delBtn);
      list.appendChild(row);
    });
  } catch (e) {
    showToast(e.message);
  }
}

document.getElementById("shopping-add").addEventListener("click", async () => {
  const input = document.getElementById("shopping-input");
  const text = input.value.trim();
  if (!text) return;
  try {
    await api("/api/shopping/add", { method: "POST", body: JSON.stringify({ text }) });
    input.value = "";
    loadShopping();
  } catch (e) {
    showToast(e.message);
  }
});

document.getElementById("shopping-clear-done").addEventListener("click", async () => {
  try {
    await api("/api/shopping/delete_done", { method: "POST", body: "{}" });
    loadShopping();
  } catch (e) {
    showToast(e.message);
  }
});

// ---------- Старт ----------

showScreen("home");
