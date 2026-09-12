# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

- Static HTML pages styled with Tailwind CSS and powered by vanilla JavaScript.
- Nginx serves the frontend and proxies browser API requests to internal services.
- FastAPI microservices provide authentication and offer matching.
- SQLite stores enterprise profiles and scraped public offers for the MVP.
- Qdrant stores and searches offer vectors.
- Docker Compose runs the local application stack.

## Users

The primary users are Moroccan enterprises looking for public tender opportunities relevant to their work, service area, and preferred offer categories.

Their main job is to avoid manually reviewing large numbers of unrelated tenders and quickly identify the opportunities most likely to fit their enterprise.

## Product Purpose

Safaqat helps an enterprise discover relevant Moroccan public tenders. An enterprise creates an account, records its business information and matching preferences, signs in, and receives its ten best currently active offers.

Success for the MVP means that an enterprise can complete signup or signin and reach a useful, understandable shortlist of active offers without constructing a manual search query.

## Positioning

Safaqat ranks opportunities from the enterprise's own profile rather than requiring the enterprise to repeatedly search the full tender catalogue. It combines exact term relevance with semantic similarity and applies business filters before presenting a personalized shortlist.

## Operating Context

1. An enterprise registers with its name, email, optional phone and legal identifier, description, keywords, categories, and locations.
2. The enterprise signs in and receives an authenticated session.
3. Safaqat filters offers by category, location, and active deadline.
4. Hybrid search compares enterprise keywords with each offer's object using sparse BM25-style relevance and dense embeddings.
5. The dashboard presents up to ten ranked offers with matching reasons and key procurement information.
6. The enterprise can inspect an offer and follow its source link.

## Capabilities and Constraints

- MVP authentication supports enterprise signup, signin, and bearer-token sessions.
- The only enterprise categories are `travaux`, `services`, and `fournitures`.
- Enterprise keywords drive hybrid search; categories and locations act as filters.
- Results exclude offers whose submission deadline has passed.
- The matching service returns no more than ten offers per request.
- Authentication data and scraped offers remain in separate SQLite databases.
- Qdrant and backend services are private on the Docker network; the frontend is the public entry point.
- The current MVP does not yet provide an AI-generated offer summary or question-answering endpoint.

## Brand Commitments

- The product name is **Safaqat**.
- Product language should be direct, trustworthy, and understandable to enterprise users.
- Do not fabricate procurement claims, guaranteed relevance, customer results, or institutional affiliations.

## Evidence on Hand

- Real scraped tender records are stored in `data/tenders.db`.
- Enterprise records are stored in `data/auth.db`.
- The working application and service configuration are under `srcs/` and `docker-compose.yaml`.
- There are no confirmed testimonials, customer logos, performance benchmarks, or official institutional endorsements; future interfaces must not invent them.

## Product Principles

1. Relevance before volume: show a short, useful set of opportunities instead of an overwhelming catalogue.
2. Explain the match: help enterprises understand why an offer appears in their results.
3. Respect procurement reality: only present active offers and preserve source information.
4. Keep the MVP focused: prioritize the signup-to-shortlist workflow before adding broader platform features.
5. Protect service boundaries: expose the frontend while keeping databases and internal services private.
