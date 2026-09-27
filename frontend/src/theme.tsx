import { useEffect, useState } from "react";
import { Icon } from "./components";
const themes = [
  ["journal", "手帐"],
  ["dark", "深色"],
  ["light", "浅色"],
  ["moonlit", "月光"],
  ["moonlit2", "月夜"],
];
export function ThemePicker() {
  const [theme, setTheme] = useState(() => {
      try {
        return localStorage.getItem("yuki_theme") || "journal";
      } catch {
        return "journal";
      }
    }),
    [open, setOpen] = useState(false);
  useEffect(() => {
    const valid = themes.some(([key]) => key === theme) ? theme : "journal";
    document.documentElement.dataset.theme = valid;
    try {
      localStorage.setItem("yuki_theme", valid);
    } catch {
      /* Optional preference. */
    }
  }, [theme]);
  return (
    <div className="theme-picker">
      <button
        className="header-button"
        aria-label="切换主题"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Icon name="moon-star" />
      </button>
      {open && (
        <div className="theme-options" role="group" aria-label="主题">
          {themes.map(([key, label]) => (
            <button
              key={key}
              aria-pressed={theme === key}
              onClick={() => {
                setTheme(key);
                setOpen(false);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
