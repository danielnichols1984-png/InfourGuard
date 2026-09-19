import { useEffect, useState } from "react";
import { api } from "../lib/api";
import "../styles/plans.css";

export default function Plans() {
  const [plans, setPlans] = useState([]);
  const [currentPlanId, setCurrentPlanId] = useState(null);
  const [switchingId, setSwitchingId] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.get("/subscriptions/plans"), api.get("/subscriptions/me")])
      .then(([plansRes, subRes]) => {
        setPlans(plansRes.data);
        setCurrentPlanId(subRes.data.plan.id);
      })
      .catch(() => setError("Couldn't load plans. Try refreshing."));
  }, []);

  const handleSwitch = async (planId) => {
    setSwitchingId(planId);
    setError("");
    try {
      await api.post("/subscriptions/me", { plan_id: planId });
      setCurrentPlanId(planId);
    } catch {
      setError("Couldn't switch plans. Try again.");
    } finally {
      setSwitchingId(null);
    }
  };

  return (
    <div className="plans-page">
      <h1>Choose your plan</h1>
      {error && <p className="plans-error">{error}</p>}
      <div className="plans-grid">
        {plans.map((plan) => {
          const isCurrent = plan.id === currentPlanId;
          return (
            <div
              key={plan.id}
              className={`plans-card${isCurrent ? " plans-card-current" : ""}`}
            >
              <h2>{plan.display_name}</h2>
              <p className="plans-price">{plan.price_display}</p>
              <ul>
                {plan.features.map((feature) => (
                  <li key={feature}>{feature}</li>
                ))}
              </ul>
              <button
                className="plans-button"
                onClick={() => handleSwitch(plan.id)}
                disabled={isCurrent || switchingId === plan.id}
              >
                {isCurrent
                  ? "Current plan"
                  : switchingId === plan.id
                    ? "Switching…"
                    : "Switch to this plan"}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
