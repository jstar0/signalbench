import { installReplay } from "./engine";

if (import.meta.env.VITE_REPLAY === "1") installReplay();
