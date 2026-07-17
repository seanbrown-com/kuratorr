(() => {
  const root = document.documentElement;
  const button = document.querySelector("[data-theme-toggle]");
  const themeColor = document.querySelector('meta[name="theme-color"]');

  const applyTheme = (theme, persist = false) => {
    root.dataset.theme = theme;
    if (themeColor) themeColor.content = theme === "dark" ? "#0F1312" : "#F0E8D9";
    if (button) {
      const nextTheme = theme === "dark" ? "light" : "dark";
      button.setAttribute("aria-label", `Switch to ${nextTheme} theme`);
      button.setAttribute("title", `Switch to ${nextTheme} theme`);
      button.querySelector("span").textContent = theme === "dark" ? "☀" : "☾";
    }
    if (persist) localStorage.setItem("kuratorr-theme", theme);
  };

  applyTheme(root.dataset.theme || "dark");
  button?.addEventListener("click", () => {
    applyTheme(root.dataset.theme === "dark" ? "light" : "dark", true);
  });
})();
