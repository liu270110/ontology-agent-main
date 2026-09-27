/// <reference types="vite/client" />

/** 构建期注入（vite.config.ts define ← package.json version / 构建时刻 ISO）。 */
declare const __APP_VERSION__: string
declare const __BUILD_TIME__: string
