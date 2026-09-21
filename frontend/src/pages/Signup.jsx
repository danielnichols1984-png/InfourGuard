import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import "../styles/login.css";

export default function Signup() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [plans, setPlans] = useState([]);
  const [planId, setPlanId] = useState(null);
  const [billing, setBilling] = useState({
    method: "cash",
    billing_name: "",
    billing_email: "",
    billing_phone: "",
    company_name: "",
    billing_address_line1: "",
    billing_address_line2: "",
    billing_city: "",
    billing_state: "",
    billing_zip: "",
    billing_country: "",
    notes: "",
  });
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    api.get("/subscriptions/plans").then((res) => {
      setPlans(res.data);
      const defaultPlan = res.data.find((p) => p.is_default);
      if (defaultPlan) setPlanId(defaultPlan.id);
    });
  }, []);

  const selectedPlan = plans.find((p) => p.id === planId);
  const needsBilling = selectedPlan && !selectedPlan.is_default;

  const updateBilling = (field) => (e) =>
    setBilling((prev) => ({ ...prev, [field]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);

    try {
      await api.post(
        "/auth/signup",
        new URLSearchParams({ email, password }),
        { headers: { Accept: "application/json" } }
      );

      await api.post(
        "/auth/login",
        new URLSearchParams({ email, password }),
        { headers: { Accept: "application/json" } }
      );

      if (needsBilling) {
        await api.post("/subscriptions/checkout", {
          plan_id: planId,
          ...billing,
        });
      }

      navigate("/dashboard");
    } catch (err) {
      if (err.response?.status === 400 && err.response?.data?.detail) {
        setError(err.response.data.detail);
      } else if (err.response?.status === 429) {
        setError("Too many attempts. Please wait a moment and try again.");
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit} noValidate>
        <h1 className="login-title">Create your account</h1>

        {error && (
          <div className="login-error" role="alert">
            {error}
          </div>
        )}

        <label className="login-field">
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </label>

        <label className="login-field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>

        <fieldset style={{ border: "none", padding: 0, margin: "12px 0" }}>
          <legend>Choose your plan</legend>
          {plans.map((p) => (
            <label key={p.id} style={{ display: "block", marginBottom: 6 }}>
              <input
                type="radio"
                name="plan"
                checked={planId === p.id}
                onChange={() => setPlanId(p.id)}
              />{" "}
              {p.display_name} — {p.price_display}
            </label>
          ))}
        </fieldset>

        {needsBilling && (
          <div style={{ marginBottom: 12 }}>
            <p style={{ fontSize: 13, color: "#666" }}>
              Your account is created right away on the Free plan; this plan
              activates once an admin confirms your cash payment.
            </p>
            <select value={billing.method} onChange={updateBilling("method")}>
              <option value="cash">Pay with cash</option>
              <option value="card" disabled>
                Credit/debit card (coming soon)
              </option>
            </select>
            <input placeholder="Full name" value={billing.billing_name} onChange={updateBilling("billing_name")} required />
            <input placeholder="Billing email" type="email" value={billing.billing_email} onChange={updateBilling("billing_email")} required />
            <input placeholder="Phone (optional)" value={billing.billing_phone} onChange={updateBilling("billing_phone")} />
            <input placeholder="Company name (optional)" value={billing.company_name} onChange={updateBilling("company_name")} />
            <input placeholder="Address line 1" value={billing.billing_address_line1} onChange={updateBilling("billing_address_line1")} required />
            <input placeholder="Address line 2 (optional)" value={billing.billing_address_line2} onChange={updateBilling("billing_address_line2")} />
            <input placeholder="City" value={billing.billing_city} onChange={updateBilling("billing_city")} required />
            <input placeholder="State/province" value={billing.billing_state} onChange={updateBilling("billing_state")} />
            <input placeholder="ZIP/postal code" value={billing.billing_zip} onChange={updateBilling("billing_zip")} />
            <input placeholder="Country" value={billing.billing_country} onChange={updateBilling("billing_country")} required />
            <textarea placeholder="Notes (optional)" value={billing.notes} onChange={updateBilling("notes")} />
          </div>
        )}

        <button type="submit" className="login-button" disabled={submitting}>
          {submitting ? "Creating account…" : "Create account"}
        </button>

        <p className="login-footer">
          Already have an account? <Link to="/login">Log in</Link>
        </p>
      </form>
    </div>
  );
}
