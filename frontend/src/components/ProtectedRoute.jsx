import { useEffect, useState } from "react";
import { Navigate, Outlet } from "react-router-dom";
import { api } from "../lib/api";

export default function ProtectedRoute() {
  const [status, setStatus] = useState("checking"); // checking | authed | anon

  useEffect(() => {
    let cancelled = false;
    api.get("/auth/me").then(
      () => {
        if (!cancelled) setStatus("authed");
      },
      () => {
        if (!cancelled) setStatus("anon");
      }
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (status === "checking") return null;
  if (status === "anon") return <Navigate to="/login" replace />;
  return <Outlet />;
}
