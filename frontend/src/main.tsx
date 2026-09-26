import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './app/App'
import { enableMockIfDev } from './mocks/enable-mock'
import './app/index.css'

async function bootstrap() {
  await enableMockIfDev()
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
}
void bootstrap()
