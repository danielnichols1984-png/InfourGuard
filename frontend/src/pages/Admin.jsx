import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import "../styles/admin.css";

const emptyForm = {
  name: "",
  display_name: "",
  price_display: "",
  features: "",
  is_default: false,
};

const featuresToList = (text) =>
  text
    .split(",")
    .map((f) => f.trim())
    .filter(Boolean);

export default function Admin() {
  const [plans, setPlans] = useState([]);
  const [users, setUsers] = useState([]);
  const [error, setError] = useState("");
  const [newPlan, setNewPlan] = useState(emptyForm);
  const [editingId, setEditingId] = useState(null);
  const [editForm, setEditForm] = useState(emptyForm);

  const loadPlans = () => api.get("/subscriptions/plans").then((res) => setPlans(res.data));
  const loadUsers = () =>
    api.get("/subscriptions/admin/users").then((res) => setUsers(res.data));

  useEffect(() => {
    Promise.all([loadPlans(), loadUsers()]).catch(() =>
      setError("Couldn't load admin data. Try refreshing.")
    );
  }, []);

  const handleCreate = async (e) => {
    e.preventDefault();
    setError("");
    try {
      await api.post("/subscriptions/admin/plans", {
        name: newPlan.name,
        display_name: newPlan.display_name,
        price_display: newPlan.price_display,
        features: featuresToList(newPlan.features),
        is_default: newPlan.is_default,
      });
      setNewPlan(emptyForm);
      await loadPlans();
    } catch (err) {
      setError(err.response?.data?.detail || "Couldn't create plan.");
    }
  };

  const startEdit = (plan) => {
    setEditingId(plan.id);
    setEditForm({
      name: plan.name,
      display_name: plan.display_name,
      price_display: plan.price_display,
      features: plan.features.join(", "),
      is_default: plan.is_default,
    });
  };

  const handleUpdate = async (planId) => {
    setError("");
    try {
      await api.put(`/subscriptions/admin/plans/${planId}`, {
        display_name: editForm.display_name,
        price_display: editForm.price_display,
        features: featuresToList(editForm.features),
        is_default: editForm.is_default,
      });
      setEditingId(null);
      await loadPlans();
    } catch (err) {
      setError(err.response?.data?.detail || "Couldn't update plan.");
    }
  };

  const handleDelete = async (planId) => {
    setError("");
    try {
      await api.delete(`/subscriptions/admin/plans/${planId}`);
      await loadPlans();
    } catch (err) {
      setError(err.response?.data?.detail || "Couldn't delete plan.");
    }
  };

  const handleUserPlanChange = async (userId, planId) => {
    setError("");
    try {
      await api.put(`/subscriptions/admin/users/${userId}/plan`, {
        plan_id: Number(planId),
      });
      await loadUsers();
    } catch {
      setError("Couldn't update that user's plan.");
    }
  };

  return (
    <div className="admin-page">
      <header className="admin-header">
        <h1>Admin</h1>
        <Link to="/dashboard">Back to dashboard</Link>
      </header>

      {error && <p className="admin-error">{error}</p>}

      <section>
        <h2>Plans</h2>
        <table className="admin-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Display name</th>
              <th>Price</th>
              <th>Features</th>
              <th>Default</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) =>
              editingId === plan.id ? (
                <tr key={plan.id}>
                  <td>{plan.name}</td>
                  <td>
                    <input
                      value={editForm.display_name}
                      onChange={(e) =>
                        setEditForm({ ...editForm, display_name: e.target.value })
                      }
                    />
                  </td>
                  <td>
                    <input
                      value={editForm.price_display}
                      onChange={(e) =>
                        setEditForm({ ...editForm, price_display: e.target.value })
                      }
                    />
                  </td>
                  <td>
                    <input
                      value={editForm.features}
                      onChange={(e) => setEditForm({ ...editForm, features: e.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={editForm.is_default}
                      onChange={(e) =>
                        setEditForm({ ...editForm, is_default: e.target.checked })
                      }
                    />
                  </td>
                  <td>
                    <button onClick={() => handleUpdate(plan.id)}>Save</button>
                    <button onClick={() => setEditingId(null)}>Cancel</button>
                  </td>
                </tr>
              ) : (
                <tr key={plan.id}>
                  <td>{plan.name}</td>
                  <td>{plan.display_name}</td>
                  <td>{plan.price_display}</td>
                  <td>{plan.features.join(", ")}</td>
                  <td>{plan.is_default ? "Yes" : ""}</td>
                  <td>
                    <button onClick={() => startEdit(plan)}>Edit</button>
                    <button onClick={() => handleDelete(plan.id)}>Delete</button>
                  </td>
                </tr>
              )
            )}
          </tbody>
        </table>

        <form className="admin-new-plan" onSubmit={handleCreate}>
          <h3>Add a plan</h3>
          <input
            placeholder="name (e.g. enterprise)"
            value={newPlan.name}
            onChange={(e) => setNewPlan({ ...newPlan, name: e.target.value })}
            required
          />
          <input
            placeholder="Display name"
            value={newPlan.display_name}
            onChange={(e) => setNewPlan({ ...newPlan, display_name: e.target.value })}
            required
          />
          <input
            placeholder="Price display (e.g. $99/mo)"
            value={newPlan.price_display}
            onChange={(e) => setNewPlan({ ...newPlan, price_display: e.target.value })}
            required
          />
          <input
            placeholder="Features, comma separated"
            value={newPlan.features}
            onChange={(e) => setNewPlan({ ...newPlan, features: e.target.value })}
          />
          <label>
            <input
              type="checkbox"
              checked={newPlan.is_default}
              onChange={(e) => setNewPlan({ ...newPlan, is_default: e.target.checked })}
            />
            Default plan
          </label>
          <button type="submit">Create plan</button>
        </form>
      </section>

      <section>
        <h2>Users</h2>
        <table className="admin-table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Plan</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.user_id}>
                <td>{u.email}</td>
                <td>
                  <select
                    value={u.plan.id}
                    onChange={(e) => handleUserPlanChange(u.user_id, e.target.value)}
                  >
                    {plans.map((plan) => (
                      <option key={plan.id} value={plan.id}>
                        {plan.display_name}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
