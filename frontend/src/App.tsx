import { NavLink, Route, Routes } from 'react-router-dom'
import AdminPage from './pages/AdminPage'
import GraphPage from './pages/GraphPage'
import ReviewPage from './pages/ReviewPage'

export default function App() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <span className="brand">学术网络治理系统</span>
        <nav>
          <NavLink to="/">关系图谱</NavLink>
          <NavLink to="/review">人工复核</NavLink>
          <NavLink to="/admin">管理后台</NavLink>
        </nav>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<GraphPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/admin" element={<AdminPage />} />
        </Routes>
      </main>
    </div>
  )
}
