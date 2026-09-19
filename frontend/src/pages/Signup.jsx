import { useState } from "react";
import axios from "axios";

export default function Signup() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const handleSignup = async () => {
    const res = await axios.post("http://127.0.0.1:8000/auth/signup", {
      email,
      password,
    });

    console.log("Signed up:", res.data);
    window.location.href = "/login";
  };

  return (
    <div>
      <h1>Signup</h1>
      <input placeholder="Email" onChange={(e) => setEmail(e.target.value)} />
      <input placeholder="Password" type="password" onChange={(e) => setPassword(e.target.value)} />
      <button onClick={handleSignup}>Signup</button>
    </div>
  );
}
