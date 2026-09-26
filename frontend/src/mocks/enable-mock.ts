/** dev 缺省启用 MSW（后端未就绪前的契约仿真）；
 *  - VITE_ENABLE_MOCK=0 关闭（切真接口）；
 *  - 生产构建（vite preview 截图验收）可经 VITE_ENABLE_MOCK=1 显式开启（构建期烘焙）。 */
export async function enableMockIfDev() {
  if (import.meta.env.VITE_ENABLE_MOCK === '0') return
  if (!import.meta.env.DEV && import.meta.env.VITE_ENABLE_MOCK !== '1') return
  const { worker } = await import('./browser')
  await worker.start({ onUnhandledRequest: 'bypass' })
}
