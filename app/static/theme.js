// Apply the saved theme before the page draws, so it doesn't flash the other one.
try { var t = localStorage.getItem("kastec-theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) {}
