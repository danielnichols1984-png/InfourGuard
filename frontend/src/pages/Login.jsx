import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import "../styles/login.css";

export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);

    try {
      await api.post(
        "/auth/login",
        new URLSearchParams({ email, password }),
        { headers: { Accept: "application/json" } }
      );
      navigate("/dashboard");
    } catch (err) {
      if (err.response?.status === 429) {
        setError("Too many attempts. Please wait a moment and try again.");
      } else if (err.response?.status === 401) {
        setError("Invalid email or password.");
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
        <h1 className="login-title">Welcome back</h1>
        <p className="login-subtitle">Sign in to your account</p>

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
            autoComplete="current-password"
            required
          />
        </label>

        <button type="submit" className="login-button" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>

        <p className="login-footer">
          Don&apos;t have an account? <Link to="/signup">Sign up</Link>
        </p>
      </form>
    </div>
  );
}
