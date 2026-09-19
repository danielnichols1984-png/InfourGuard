import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import "../styles/dashboard.css";

export default function Dashboard() {
  const [user, setUser] = useState(null);
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.get("/auth/me"), api.get("/subscriptions/me")])
      .then(([userRes, subRes]) => {
        setUser(userRes.data);
        setPlan(subRes.data.plan);
      })
      .catch(() => setError("Couldn't load your account. Try refreshing."));
  }, []);

  const handleLogout = async () => {
    await api.post("/auth/logout", null, { headers: { Accept: "application/json" } });
    window.location.href = "/login";
  };

  if (error) {
    return (
      <div className="dashboard-page">
        <p className="dashboard-error">{error}</p>
      </div>
    );
  }

  if (!user || !plan) {
    return <div className="dashboard-page">Loading…</div>;
  }

  return (
    <div className="dashboard-page">
      <header className="dashboard-header">
        <h1>Welcome back, {user.email}</h1>
        <div className="dashboard-header-actions">
          {user.is_admin && (
            <Link to="/admin/plans" className="dashboard-admin-link">
              Admin
            </Link>
          )}
          <button className="dashboard-logout" onClick={handleLogout}>
            Log out
          </button>
        </div>
      </header>

      <div className="dashboard-plan-card">
        <div>
          <span className="dashboard-plan-label">Current plan</span>
          <h2>{plan.display_name}</h2>
          <p className="dashboard-plan-price">{plan.price_display}</p>
        </div>
        <Link to="/plans" className="dashboard-plan-link">
          Manage plan
        </Link>
      </div>
    </div>
  );
}
