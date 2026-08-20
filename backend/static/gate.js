/* 規格頁的草稿保存與即時進度。
 *
 * 來源是 SpecGate prototype（docs/prototype/KAN-15.html）的同一套作法：
 * 每次改動寫進 localStorage，中途關頁面不會掉答案。
 *
 * 版本綁定很重要：報告改版後 slug 可能換了一批，套用舊草稿會把答案錯置，
 * 所以 key 帶上版本，且只在版本相符時才還原。
 */
(function () {
  "use strict";

  var form = document.getElementById("gateform");
  if (!form || form.dataset.frozen === "1") return;

  var KEY = "devloop-draft:" + form.dataset.card + ":" + form.dataset.version;
  var hint = document.getElementById("draft-hint");
  var progress = document.getElementById("progress-count");
  var freezeBtn = document.getElementById("freeze-btn");

  function fields() {
    return form.querySelectorAll("input[name], textarea[name], select[name]");
  }

  /* ---------- 草稿 ---------- */

  function collect() {
    var data = {};
    fields().forEach(function (el) {
      if (el.type === "radio") {
        if (el.checked) data[el.name] = el.value;
      } else {
        data[el.name] = el.value;
      }
    });
    return data;
  }

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify({ at: Date.now(), data: collect() }));
      if (hint) {
        hint.textContent =
          "· 草稿已存 " +
          new Date().toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit" });
      }
    } catch (e) {
      /* 無痕模式之類寫不進去 —— 不要因此弄壞頁面 */
    }
  }

  function restore() {
    var raw;
    try {
      raw = localStorage.getItem(KEY);
    } catch (e) {
      return;
    }
    if (!raw) return;

    var saved;
    try {
      saved = JSON.parse(raw).data || {};
    } catch (e) {
      return; /* 草稿壞掉就當沒有 */
    }

    var restored = false;
    fields().forEach(function (el) {
      if (!(el.name in saved)) return;
      if (el.type === "radio") {
        if (el.value === saved[el.name] && !el.checked) {
          el.checked = true;
          restored = true;
        }
      } else if (!el.value && saved[el.name]) {
        el.value = saved[el.name];
        restored = true;
      }
    });
    if (restored && hint) hint.textContent = "· 已還原上次未送出的草稿";
  }

  function clear() {
    try {
      localStorage.removeItem(KEY);
    } catch (e) {
      /* 清不掉也無所謂，版本一換 key 就不同了 */
    }
  }

  /* ---------- 即時進度 ---------- */

  function isAnswered(box) {
    var slug = box.dataset.slug;
    var note = form.querySelector('[name="note__' + slug + '"]');
    var noteText = note ? note.value.trim() : "";

    var chosen = form.querySelector('input[name="choice__' + slug + '"]:checked');
    if (chosen) {
      // 「以上皆非」要有說明才算數 —— 跟後端三層驗證同一條規則
      return chosen.value === "none" ? noteText.length > 0 : true;
    }
    if (form.querySelector('input[name="value__' + slug + '"]:checked')) return true;

    var text = form.querySelector('[name="text__' + slug + '"]');
    return !!(text && text.value.trim());
  }

  function paint() {
    var boxes = form.querySelectorAll(".q[data-required='1']");
    var done = 0;

    form.querySelectorAll(".q").forEach(function (box) {
      var ok = isAnswered(box);
      box.classList.toggle("answered", ok);
      if (ok && box.dataset.required === "1") done += 1;

      var chosen = form.querySelector(
        'input[name="choice__' + box.dataset.slug + '"]:checked'
      );
      var needsNote = !!chosen && chosen.value === "none";
      var label = box.querySelector(".note-label");
      if (label) {
        label.classList.toggle("must", needsNote);
        label.textContent = needsNote ? "必填 —— 你要的是什麼？" : "備註（可留空）";
      }
    });

    form.querySelectorAll("label.opt").forEach(function (label) {
      var input = label.querySelector("input");
      label.classList.toggle("sel", !!input && input.checked);
    });
    form.querySelectorAll(".yn label").forEach(function (label) {
      var input = label.querySelector("input");
      var on = !!input && input.checked;
      label.classList.toggle("sel-y", on && input.value === "yes");
      label.classList.toggle("sel-n", on && input.value === "no");
    });

    if (progress) progress.textContent = String(done);
    if (freezeBtn) {
      var complete = done >= boxes.length;
      freezeBtn.disabled = !complete;
      freezeBtn.title = complete ? "" : "還有題目沒答完";
    }
  }

  /* ---------- 綁事件 ---------- */

  form.addEventListener("input", function () {
    paint();
    save();
  });
  form.addEventListener("change", function () {
    paint();
    save();
  });
  form.addEventListener("submit", function (e) {
    // 凍結成功之後草稿就沒用了；儲存答案則留著，回來還看得到
    if (e.submitter && e.submitter.id === "freeze-btn") clear();
  });

  restore();
  paint();
})();
