import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";

const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
try { tg?.setHeaderColor?.("#102f36"); tg?.setBackgroundColor?.("#102f36"); } catch { /* older Telegram clients */ }

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
