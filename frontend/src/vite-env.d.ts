/// <reference types="vite/client" />
interface Window { Telegram?: { WebApp?: { initData: string; ready(): void; expand(): void; setHeaderColor?(color: string): void; setBackgroundColor?(color: string): void; BackButton?: { show(): void; hide(): void; onClick(cb:()=>void): void }; HapticFeedback?: { impactOccurred(style:string):void; notificationOccurred(type:string):void } } } }
