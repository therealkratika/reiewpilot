import "dotenv/config";
import express from "express";
import cors from "cors";
import helmet from "helmet";
import { z } from "zod";

const app = express();
const port = Number(process.env.API_PORT ?? 4000);

app.disable("x-powered-by");
app.use(helmet());
app.use(cors({ origin: process.env.WEB_ORIGIN ?? "http://localhost:3000" }));
app.use(express.json({ limit: "1mb" }));

app.get("/health", (_req, res) => {
  res.status(200).json({
    status: "ok",
    service: "reviewpilot-api",
    timestamp: new Date().toISOString()
  });
});

const reviewRequestSchema = z.object({
  repository: z.string().min(1).max(200),
  pullRequestNumber: z.number().int().positive(),
  headSha: z.string().min(7).max(64)
});

app.post("/api/reviews", (req, res) => {
  const parsed = reviewRequestSchema.safeParse(req.body);

  if (!parsed.success) {
    return res.status(400).json({
      error: "Invalid review request",
      details: parsed.error.flatten()
    });
  }

  return res.status(202).json({
    message: "Review request accepted by starter API",
    request: parsed.data,
    status: "queued_placeholder"
  });
});

app.listen(port, () => {
  console.log(`ReviewPilot API listening on http://localhost:${port}`);
});
