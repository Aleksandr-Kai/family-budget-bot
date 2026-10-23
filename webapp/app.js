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

const screens = ["home", "expenses", "categories", "shopping"];
const titles = {
  home: "Главная",
  expenses: "Расходы",
  categories: "Категории",
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
  if (name === "expenses") loadExpenses(currentExpenseScope);
  if (name === "categories") loadCategories();
  if (name === "shopping") loadShopping();
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => showScreen(btn.dataset.screen));
});

// ---------- Главная ----------

async function loadDashboard() {
  try {
    const [d, budgets] = await Promise.all([api("/api/dashboard"), api("/api/budgets")]);

    document.getElementById("shared-remaining").textContent = fmt(d.shared_remaining);
    document.getElementById("shared-breakdown").textContent =
      `Бюджет ${fmt(d.shared_budget_total)} − потрачено ${fmt(d.shared_spent)} (с ${d.period_start})`;

    if (d.personal_budget === null) {
      document.getElementById("personal-remaining").textContent = "не задан";
      document.getElementById("personal-breakdown").textContent = "Задайте сумму ниже";
    } else {
      document.getElementById("personal-remaining").textContent = fmt(d.personal_remaining);
      document.getElementById("personal-breakdown").textContent =
        `Бюджет ${fmt(d.personal_budget)} − потрачено ${fmt(d.personal_spent)}`;
    }

    document.getElementById("today-total").textContent = fmt(d.today_total);

    const list = document.getElementById("all-budgets");
    list.innerHTML = "";
    budgets.budgets.forEach((b) => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `<span>${b.display_name || "?"}</span><span class="amount">${fmt(b.amount)}</span>`;
      list.appendChild(row);
    });
  } catch (e) {
    showToast(e.message);
  }
}

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

let currentExpenseScope = "period";

document.querySelectorAll('#screen-expenses .tab').forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll('#screen-expenses .tab').forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentExpenseScope = tab.dataset.scope;
    loadExpenses(currentExpenseScope);
  });
});

async function loadExpenses(scope) {
  const list = document.getElementById("expenses-list");
  list.innerHTML = "";
  try {
    const data = await api(`/api/expenses?scope=${scope}`);
    document.getElementById("expenses-total").textContent = `Итого: ${fmt(data.total)}`;
    if (!data.items.length) {
      list.innerHTML = '<div class="empty">Расходов пока нет</div>';
      return;
    }
    data.items.forEach((item) => {
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
      list.appendChild(row);
    });
  } catch (e) {
    showToast(e.message);
  }
}

// ---------- Категории ----------

document.querySelectorAll('#screen-categories .tab').forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll('#screen-categories .tab').forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const view = tab.dataset.view;
    document.getElementById("categories-period").classList.toggle("hidden", view !== "period");
    document.getElementById("categories-months").classList.toggle("hidden", view !== "months");
  });
});

function renderTotalsList(container, totals) {
  container.innerHTML = "";
  if (!totals.length) {
    container.innerHTML = '<div class="empty">Нет данных</div>';
    return;
  }
  totals.forEach((t) => {
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<span>${t.category}</span><span class="amount">${fmt(t.amount)}</span>`;
    container.appendChild(row);
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
      row.innerHTML = `
        <input type="checkbox" ${item.done ? "checked" : ""} data-name="${item.item}">
        <span class="item-text">${item.item}</span>
        <button class="del-btn" data-name="${item.item}">✕</button>`;
      list.appendChild(row);
    });

    list.querySelectorAll('input[type=checkbox]').forEach((cb) => {
      cb.addEventListener("change", async () => {
        try {
          await api("/api/shopping/done", {
            method: "POST",
            body: JSON.stringify({ names: [cb.dataset.name] }),
          });
          loadShopping();
        } catch (e) {
          showToast(e.message);
        }
      });
    });
    list.querySelectorAll('.del-btn').forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api("/api/shopping/delete", {
            method: "POST",
            body: JSON.stringify({ names: [btn.dataset.name] }),
          });
          loadShopping();
        } catch (e) {
          showToast(e.message);
        }
      });
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
