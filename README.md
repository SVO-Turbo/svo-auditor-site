# SVO Turbo: SEO Auditor Interface

## 1. Project Overview
This repository contains the public-facing marketing and auditing interface for the SVO Turbo framework. Operating as the primary entry point for client acquisition and initial visibility assessment, this application connects directly to the broader Digital Authority Platform (DAP) architecture.

The site is engineered for maximum performance, utilizing edge-network deployment to ensure zero-latency interactions and strict adherence to technical SEO best practices.

---

## 2. Technical Stack
* Framework: Next.js (App Router)
* Hosting and Delivery: Vercel (Global Edge Network)
* Styling: Tailwind CSS
* Analytics: Privacy-first, cookie-less telemetry

---

## 3. Operational Features

### 3.1 The Visibility Auditor
A diagnostic engine that assesses baseline Search Visibility Optimization (SVO) metrics. It bypasses superficial metrics to evaluate core data pipeline integrity across the primary search engines (Google, Bing, Apple).

### 3.2 Lead Orchestration
Capture forms and audit requests are decoupled from the frontend, routing payloads directly to the internal AWS infrastructure for processing and CRM ingestion. 

---

## 4. Local Development Protocol

To establish the local environment for UI adjustments or component testing:

```bash
# 1. Clone the repository
git clone [https://github.com/SVO-Turbo/seo-auditor-site.git](https://github.com/SVO-Turbo/seo-auditor-site.git)

# 2. Navigate to the project directory
cd seo-auditor-site

# 3. Install dependencies
npm install

# 4. Initialize the local development server
npm run dev
