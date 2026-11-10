import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

const demoMode = import.meta.env.VITE_DEMO_MODE === "true";
document.documentElement.classList.toggle("demo-mode", demoMode);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {demoMode && <div className="demo-notice" role="note">离线演示 · 规则与示例资料，不调用模型。请勿填写真实健康信息。</div>}
    <App />
  </React.StrictMode>,
);

