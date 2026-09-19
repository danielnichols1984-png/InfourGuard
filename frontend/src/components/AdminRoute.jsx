import { useEffect, useState } from "react";
import { Navigate, Outlet } from "react-router-dom";
import { api } from "../lib/api";

export default function AdminRoute() {
  const [status, setStatus] = useState("checking"); // checking | admin | anon | denied

  useEffect(() => {
    let cancelled = false;
    api.get("/auth/me").then(
      (res) => {
        if (cancelled) return;
        setStatus(res.data.is_admin ? "admin" : "denied");
      },
      (err) => {
        if (cancelled) return;
        setStatus(err.response?.status === 401 ? "anon" : "denied");
      }
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (status === "checking") return null;
  if (status === "anon") return <Navigate to="/login" replace />;
  if (status === "denied") return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}
