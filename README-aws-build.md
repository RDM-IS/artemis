# Artemis — AWS Build (v3.1)

This branch is the v3.1 architectural rebuild targeting AWS EC2.
The `main` branch contains the original tower/WSL2 build and remains
production until this branch is promoted.

## Branch strategy
- `main` — tower build, current prod, do not modify
- `aws-build` — this branch, AWS rebuild in progress

## Environment
Copy `.env.example.aws` to `.env` and populate all values before running.
Never commit `.env`.

## Status
Phase 0 — Foundation integrity (bug fixes)
