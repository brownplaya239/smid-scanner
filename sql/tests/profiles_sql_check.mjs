// Checks sql/2026-11-16_profiles_for_every_account.sql on a throwaway
// in-memory Postgres (PGlite) that mimics the parts of Supabase it touches:
// the auth.users table, the anon / authenticated / service_role roles, row
// security, and PR #8's plan guard.
//
//   bun install --cwd sql/tests
//   node sql/tests/profiles_sql_check.mjs [path/to/pr8_lock.sql]
//
// With no argument it uses sql/2026-11-10_lock_plans_and_report_usage.sql if
// that file exists (PR #8 merged), else a built-in copy of PR #8's guard
// trigger. Prints one PASS/FAIL line per check and exits 1 on any failure.
// profiles_sql_test.py runs it under pytest (skipped when PGlite is not
// installed, as in CI).
import { PGlite } from "@electric-sql/pglite";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { BASE_SCHEMA } from "./supabase_schema.mjs";

const SQL_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const PR17 = readFileSync(join(SQL_DIR, "2026-11-16_profiles_for_every_account.sql"), "utf8");
const pr8Path = process.argv[2] || join(SQL_DIR, "2026-11-10_lock_plans_and_report_usage.sql");
const HAVE_PR8_FILE = existsSync(pr8Path);

// PR #8's guard trigger, as on its branch (used when its file isn't here).
const GUARD_SQL = `
alter table public.profiles
  add column if not exists subscription_expires_at timestamptz,
  add column if not exists promo_code_redeemed     text,
  add column if not exists promo_code_redeemed_at  timestamptz,
  add column if not exists stripe_customer_id      text,
  add column if not exists beta_cohort_lock        numeric;
create or replace function public.td_guard_profile_plan_fields()
returns trigger language plpgsql set search_path = public as $$
begin
  if current_user not in ('authenticated', 'anon') then return new; end if;
  if tg_op = 'INSERT' then
    new.subscription_tier := 'free'; new.subscription_expires_at := null;
    new.promo_code_redeemed := null; new.promo_code_redeemed_at := null;
    new.stripe_customer_id := null; new.beta_cohort_lock := null;
    return new;
  end if;
  if new.subscription_tier is distinct from old.subscription_tier
  or new.subscription_expires_at is distinct from old.subscription_expires_at
  or new.promo_code_redeemed is distinct from old.promo_code_redeemed
  or new.promo_code_redeemed_at is distinct from old.promo_code_redeemed_at
  or new.stripe_customer_id is distinct from old.stripe_customer_id
  or new.beta_cohort_lock is distinct from old.beta_cohort_lock then
    raise exception 'Plan fields can only be changed by TickerDesk billing.' using errcode = '42501';
  end if;
  return new;
end; $$;
drop trigger if exists td_guard_profile_plan_fields on public.profiles;
create trigger td_guard_profile_plan_fields before insert or update on public.profiles
  for each row execute function public.td_guard_profile_plan_fields();
`;
const PR8 = HAVE_PR8_FILE ? readFileSync(pr8Path, "utf8") : GUARD_SQL;

const db = new PGlite();
let pass = 0;
let fail = 0;
function check(name, ok, detail) {
  if (ok) pass++;
  else fail++;
  console.log((ok ? "PASS " : "FAIL ") + name + (!ok && detail ? "  -- " + detail : ""));
}
const q = async (sql, params) => (await db.query(sql, params)).rows;
const one = async (sql, params) => (await q(sql, params))[0];
async function err(sql, params) {
  try {
    await db.query(sql, params);
    return null;
  } catch (e) {
    return e.message;
  }
}
async function as(role, uid, fn) {
  await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub', '${uid || ""}', false);`);
  try {
    return await fn();
  } finally {
    await db.exec("reset role;");
  }
}
const runPr17 = async () => (await db.exec(PR17)).at(-1).rows[0];
const U = (n) => "00000000-0000-4000-8000-" + String(n).padStart(12, "0");
const wl = async (id) =>
  (await q(`select ticker from public.watchlists where user_id = $1 order by ticker`, [id])).map((r) => r.ticker).join(",");
const prof = (id) => one(`select * from public.profiles where id = $1`, [id]);
const STARTERS = "AAPL,AVGO,MRVL,MU,NVDA";

// ── Supabase-like schema, as live before this script
await db.exec(BASE_SCHEMA);

// ── Accounts as they are before the script ─────────────────────────────
// 1 paid Stripe Premium (profile, 2 tickers)     2 live Premium promo (profile)
// 3 hand-granted beta (profile)                  4..8 no profile, empty watchlist
// 9 no profile, has tickers                     10 profile the SITE created before
//                                                  the script ran: starter_seeded
//                                                  missing, empty watchlist
for (let n = 1; n <= 10; n++) {
  await db.query(`insert into auth.users (id, email, created_at) values ($1, $2, now() - interval '100 days')`, [U(n), `u${n}@x`]);
}
await db.exec(`
insert into public.profiles (id, subscription_tier, stripe_customer_id) values ('${U(1)}', 'premium', 'cus_1');
insert into public.profiles (id, subscription_tier, subscription_expires_at, promo_code_redeemed)
  values ('${U(2)}', 'premium', now() + interval '10 days', 'SPRING');
insert into public.profiles (id, subscription_tier) values ('${U(3)}', 'beta');
insert into public.watchlists (user_id, ticker) values ('${U(1)}', 'TSLA'), ('${U(1)}', 'AMD'), ('${U(9)}', 'PLTR');
insert into public.profiles (id) values ('${U(10)}');
`);

// ── Run order A: PR #8 first, then this script twice ───────────────────
console.log(`(PR #8 SQL: ${HAVE_PR8_FILE ? pr8Path : "built-in guard copy"})`);
await db.exec(PR8);
const first = await runPr17();
check("first run: every account has a profile", Number(first.accounts_without_profile) === 0, JSON.stringify(first));
check("first run: creates the 6 missing profiles", Number(first.profiles_created_by_this_run) === 6, JSON.stringify(first));
check("first run: trigger + Stripe sync installed", first.signup_trigger_installed && first.stripe_sync_installed, JSON.stringify(first));
// Only accounts 4-8: they had no profile (allow-listed) and an empty
// watchlist. Accounts 2, 3 and 10 already had a profile, so their empty
// watchlists are left as they are (finding 5: maybe emptied on purpose).
check("first run: 5 starter watchlists, only for accounts that had no profile",
  Number(first.starter_watchlists_added_by_this_run) === 5, JSON.stringify(first));
const allow = (await q(`select user_id, reason from public.starter_repair_allowlist order by user_id`))
  .map((r) => r.user_id.slice(-2) + ":" + r.reason).join(",");
check("allowlist = the 6 accounts with no profile before the run (4-9)",
  allow === [4, 5, 6, 7, 8, 9].map((n) => String(n).padStart(2, "0") + ":no_profile_at_migration").join(","), allow);
check("Stripe customer cached on a profile is back-filled into stripe_customers",
  (await one(`select count(*)::int n from public.stripe_customers where user_id = $1 and customer_id = 'cus_1'`, [U(1)])).n === 1);
for (const n of [4, 5, 6, 7, 8]) {
  const p = await prof(U(n));
  check(`account ${n}: Free profile, starters, starter_seeded`,
    p.subscription_tier === "free" && p.starter_seeded === true && (await wl(U(n))) === STARTERS, JSON.stringify(p));
}
const p4 = await prof(U(4));
check("backfilled profile keeps the account's sign-up date", Date.now() - new Date(p4.created_at).getTime() > 90 * 864e5);
for (const n of [2, 3, 10]) {
  check(`account ${n}: had a profile, empty watchlist stays empty (finding 5)`,
    (await wl(U(n))) === "" && (await prof(U(n))).starter_seeded !== true);
}
check("account with its own tickers is left alone", (await wl(U(9))) === "PLTR" && (await prof(U(9))).starter_seeded === false);
check("paid account: plan and tickers untouched",
  (await prof(U(1))).subscription_tier === "premium" && (await wl(U(1))) === "AMD,TSLA");
const p2 = await prof(U(2));
check("promo account: plan and end date untouched", p2.subscription_tier === "premium" && p2.subscription_expires_at !== null);

const second = await runPr17();
check("re-run creates nothing and seeds nothing",
  Number(second.profiles_created_by_this_run) === 0 && Number(second.starter_watchlists_added_by_this_run) === 0,
  JSON.stringify(second));
// 3 own tickers + 5 starters for each of the 5 seeded accounts.
const wlCount = (await one(`select count(*)::int n from public.watchlists`)).n;
check("watchlist row count is exactly 3 own + 5 x 5 starters", wlCount === 28, String(wlCount));

// A user who deleted all starters (starter_seeded = true) is not re-seeded.
await db.exec(`delete from public.watchlists where user_id = '${U(4)}'`);
await runPr17();
check("emptied watchlist with starter_seeded = true stays empty", (await wl(U(4))) === "");
// Repair is safe to re-run on accounts that still have no flag but now have tickers.
check("accounts with tickers never get starters added", (await wl(U(9))) === "PLTR");

// ── Signup trigger ─────────────────────────────────────────────────────
await db.query(`insert into auth.users (id) values ($1)`, [U(20)]);
const p20 = await prof(U(20));
check("new signup: Free profile + 5 starters + flag", p20?.subscription_tier === "free" && p20.starter_seeded === true && (await wl(U(20))) === STARTERS);
// A failing watchlist insert must not block signup, and the next run repairs it.
await db.exec(`alter table public.watchlists add constraint td_test_block check (ticker <> 'NVDA') not valid`);
const signupErr = await err(`insert into auth.users (id) values ('${U(21)}')`);
check("broken starter seed does not block signup", signupErr === null, signupErr);
const p21 = await prof(U(21));
check("...profile still created, not marked seeded", p21?.subscription_tier === "free" && p21.starter_seeded === false && (await wl(U(21))) === "");
check("...and the trigger put it on the allowlist",
  (await one(`select reason from public.starter_repair_allowlist where user_id = $1`, [U(21)]))?.reason === "signup_seed_failed");
await db.exec(`alter table public.watchlists drop constraint td_test_block`);
const repair = await runPr17();
check("...next run repairs that account's starters", (await wl(U(21))) === STARTERS && (await prof(U(21))).starter_seeded === true, JSON.stringify(repair));
// Repair fails as a whole (no flag set) if the insert fails.
await db.query(`insert into auth.users (id) values ($1)`, [U(22)]);
await db.exec(`delete from public.watchlists where user_id = '${U(22)}'; update public.profiles set starter_seeded = false where id = '${U(22)}';
  alter table public.watchlists add constraint td_test_block check (ticker <> 'NVDA') not valid`);
const repairErr = await err(PR17);
await db.exec(`alter table public.watchlists drop constraint td_test_block`);
check("failed repair insert never marks starter_seeded", repairErr !== null && (await prof(U(22))).starter_seeded === false, repairErr);

// ── Browser access (with PR #8's guard) ───────────────────────────────
await db.query(`insert into auth.users (id) values ($1)`, [U(30)]);
await db.exec(`delete from public.profiles where id = '${U(30)}'`); // an account the trigger missed
const ens = await as("authenticated", U(30), () =>
  err(`insert into public.profiles (id, last_seen) values ('${U(30)}', now()) on conflict (id) do nothing`));
check("site ensureProfile() insert works for the signed-in user", ens === null && (await prof(U(30)))?.subscription_tier === "free", ens);
await db.exec(`delete from public.profiles where id = '${U(30)}'`);
await as("authenticated", U(30), () =>
  err(`insert into public.profiles (id, subscription_tier) values ('${U(30)}', 'premium') on conflict (id) do nothing`));
check("browser insert claiming Premium still lands on Free", (await prof(U(30)))?.subscription_tier === "free");
const other = await as("authenticated", U(30), () => err(`insert into public.profiles (id) values ('${U(31)}')`));
check("browser cannot create someone else's profile", other !== null, other);
const FUNCS = [
  `public.td_apply_stripe_state('${U(30)}', 'premium', 'cus_x', gen_random_uuid())`,
  `public.td_acquire_reconcile_lease('${U(30)}', gen_random_uuid(), 60)`,
  `public.td_release_reconcile_lease('${U(30)}', gen_random_uuid())`,
];
const TABLES = {
  stripe_webhook_events: "(event_id) values ('evt_fake')",
  stripe_customers: `(user_id, customer_id) values ('${U(30)}', 'cus_fake')`,
  stripe_reconcile_lease: `(user_id, token, expires_at) values ('${U(30)}', gen_random_uuid(), now() + interval '100 years')`,
  starter_repair_allowlist: `(user_id, reason) values ('${U(30)}', 'x')`,
  td_migration_marks: "(name) values ('x')",
};
for (const role of ["authenticated", "anon"]) {
  for (const f of FUNCS) {
    const e = await as(role, U(30), () => err(`select ${f}`));
    check(`${role} cannot execute ${f.split("(")[0]}`, e && /permission denied/.test(e), e);
  }
  for (const [t, ins] of Object.entries(TABLES)) {
    const e2 = await as(role, U(30), () => err(`select * from public.${t}`));
    check(`${role} cannot read ${t}`, e2 && /permission denied/.test(e2), e2);
    const e3 = await as(role, U(30), () => err(`insert into public.${t} ${ins}`));
    check(`${role} cannot write ${t}`, e3 && /permission denied/.test(e3), e3);
  }
}

// ── Lease + td_apply_stripe_state ──────────────────────────────────────
const svc = (sql, params) => as("service_role", null, () => one(sql, params));
const acquire = (uid, tok, ttl = 60) =>
  svc(`select public.td_acquire_reconcile_lease($1, $2, $3) r`, [uid, tok, ttl]).then((r) => r.r);
const release = (uid, tok) => svc(`select public.td_release_reconcile_lease($1, $2) r`, [uid, tok]).then((r) => r.r);
const applyTok = (uid, tier, tok, cus = null, evt = null) =>
  svc(`select public.td_apply_stripe_state($1, $2, $3, $4, $5, 'test') r`, [uid, tier, cus, tok, evt]).then((r) => r.r);
const tok = () => crypto.randomUUID();
// One full sync, as the worker does it: lease, save, give back.
async function apply(uid, tier, cus = null, evt = null) {
  const t = tok();
  if (!(await acquire(uid, t))) throw new Error("lease busy for " + uid);
  try {
    return await applyTok(uid, tier, t, cus, evt);
  } finally {
    await release(uid, t);
  }
}
const leaseRow = (uid) => one(`select token, expires_at from public.stripe_reconcile_lease where user_id = $1`, [uid]);

// Missing profile + paid state: the row is created with the plan.
await db.query(`insert into auth.users (id) values ($1)`, [U(40)]);
await db.exec(`delete from public.profiles where id = '${U(40)}'`);
const a1 = await apply(U(40), "pro", "cus_40", "evt_a");
const p40 = await prof(U(40));
check("missing profile: created with the paid plan", a1.applied && p40.subscription_tier === "pro" && p40.stripe_customer_id === "cus_40", JSON.stringify(a1));
check("event id recorded", (await one(`select count(*)::int n from public.stripe_webhook_events where event_id = 'evt_a'`)).n === 1);
check("customer recorded in stripe_customers", (await one(`select count(*)::int n from public.stripe_customers where user_id = $1 and customer_id = 'cus_40'`, [U(40)])).n === 1);
check("lease given back after the save", (await leaseRow(U(40))) === undefined);

// Saving needs the lease.
const noLease = await applyTok(U(40), "free", tok(), "cus_40");
check("save without holding the lease is refused (lease_lost)", !noLease.applied && noLease.reason === "lease_lost" && (await prof(U(40))).subscription_tier === "pro", JSON.stringify(noLease));

// Finding 1, the reproduced interleaving: A takes the lease and is slow
// reading Stripe; B (which read the older Pro state) cannot take the lease
// and cannot save; A saves Premium. B's retry later reads the newer state.
const tA = tok();
const tB = tok();
check("A takes the lease", (await acquire(U(40), tA)) === true);
check("B cannot take a live lease", (await acquire(U(40), tB)) === false);
const bSave = await applyTok(U(40), "pro", tB, "cus_40");
check("B cannot save with a lease it doesn't hold", !bSave.applied && bSave.reason === "lease_lost", JSON.stringify(bSave));
const aSave = await applyTok(U(40), "premium", tA, "cus_40");
check("A saves Premium", aSave.applied && (await prof(U(40))).subscription_tier === "premium", JSON.stringify(aSave));
check("B cannot release A's lease", (await release(U(40), tB)) === false && (await leaseRow(U(40)))?.token === tA);
check("A releases", (await release(U(40), tA)) === true && (await leaseRow(U(40))) === undefined);
const bRetry = await apply(U(40), "premium", "cus_40");
check("B's retry (reading Premium now) saves; account ends on Premium",
  bRetry.applied && (await prof(U(40))).subscription_tier === "premium", JSON.stringify(bRetry));

// An expired lease can be taken over, and the old holder can no longer save.
const tOld = tok();
const tNew = tok();
await acquire(U(40), tOld);
await db.exec(`update public.stripe_reconcile_lease set expires_at = now() - interval '1 second' where user_id = '${U(40)}'`);
const oldExpired = await applyTok(U(40), "free", tOld, "cus_40");
check("holder of an expired lease cannot save", !oldExpired.applied && oldExpired.reason === "lease_lost" && (await prof(U(40))).subscription_tier === "premium", JSON.stringify(oldExpired));
check("an expired lease can be taken over", (await acquire(U(40), tNew)) === true && (await leaseRow(U(40)))?.token === tNew);
const oldAfter = await applyTok(U(40), "free", tOld, "cus_40");
check("old holder still cannot save after the takeover", !oldAfter.applied && (await prof(U(40))).subscription_tier === "premium");
check("old holder's release does not free the new lease", (await release(U(40), tOld)) === false && (await leaseRow(U(40)))?.token === tNew);
await release(U(40), tNew);

// A lease that is never given back (lost release) expires on its own.
const tLost = tok();
check("short lease taken", (await acquire(U(40), tLost, 1)) === true);
check("...blocks others while live", (await acquire(U(40), tok())) === false);
await new Promise((r) => setTimeout(r, 1200));
const tNext = tok();
check("...and can be taken once it has expired (lost release)", (await acquire(U(40), tNext)) === true);
await release(U(40), tNext);
const badTtl = await as("service_role", null, () => err(`select public.td_acquire_reconcile_lease('${U(40)}', gen_random_uuid(), 0)`));
check("lease length must be 1..600 seconds", badTtl && /invalid arguments/.test(badTtl), badTtl);

// Paid → cancelled → Free.
const a4 = await apply(U(40), "free", "cus_40");
check("Stripe 'free' downgrades a Stripe plan", a4.applied && (await prof(U(40))).subscription_tier === "free", JSON.stringify(a4));

// Existing paid row: a customer-id-only write must not touch the plan.
await as("service_role", null, () => db.exec(`update public.profiles set stripe_customer_id = 'cus_1b' where id = '${U(1)}'`));
check("existing paid row keeps its plan when only the customer id changes", (await prof(U(1))).subscription_tier === "premium");

// Promo row: a paid Stripe state replaces the promo and clears its end date.
const a5 = await apply(U(2), "pro", "cus_2");
const p2b = await prof(U(2));
check("promo user who pays: Stripe plan, promo end date cleared", a5.applied && p2b.subscription_tier === "pro" && p2b.subscription_expires_at === null && p2b.promo_code_redeemed === "SPRING", JSON.stringify(p2b));
// Live promo + Stripe 'free' (e.g. a late event for an old cancelled sub): promo kept.
await db.query(`insert into auth.users (id) values ($1)`, [U(41)]);
await db.exec(`update public.profiles set subscription_tier = 'premium', subscription_expires_at = now() + interval '5 days' where id = '${U(41)}'`);
const a6 = await apply(U(41), "free", "cus_41");
const p41 = await prof(U(41));
check("live promo is kept when Stripe says not paying", p41.subscription_tier === "premium" && p41.subscription_expires_at !== null && a6.reason === "kept_non_stripe_plan", JSON.stringify(a6));
const a7 = await apply(U(3), "free", "cus_3");
check("hand-granted beta is kept when Stripe says not paying", (await prof(U(3))).subscription_tier === "beta", JSON.stringify(a7));
const bad = await as("service_role", null, () => err(`select public.td_apply_stripe_state('${U(3)}', 'beta', null, gen_random_uuid())`));
check("only free / pro / premium accepted", bad && /invalid arguments/.test(bad), bad);
const ghost = await as("service_role", null, () => err(`select public.td_acquire_reconcile_lease('${U(99)}', gen_random_uuid(), 60)`));
check("unknown account errors (worker answers 500, Stripe retries)", ghost !== null, ghost);

// Row locks: profile row (like PR #8's td_redeem_promo) and lease row, held until commit.
const tLock = tok();
await acquire(U(41), tLock);
await db.exec("begin; set local role service_role;");
await db.query(`select public.td_apply_stripe_state('${U(41)}', 'pro', null, '${tLock}')`);
await db.exec("reset role");
const locked = await q(`select c.relname from pg_locks l join pg_class c on c.oid = l.relation
  where l.locktype = 'relation' and l.mode = 'RowShareLock' and c.relname in ('profiles', 'stripe_reconcile_lease')`);
await db.exec("rollback");
await release(U(41), tLock);
const names = locked.map((r) => r.relname).sort().join(",");
check("takes FOR UPDATE row locks on the profile and the lease", names === "profiles,stripe_reconcile_lease", names);
check("rollback leaves the promo untouched", (await prof(U(41))).subscription_tier === "premium");

// Works with PR #8's promo function, if that SQL defines it.
if ((await one(`select to_regprocedure('public.td_redeem_promo(uuid, text, text, integer)') is not null ok`)).ok) {
  await apply(U(40), "premium", "cus_40");
  const r = await as("service_role", null, () => one(`select public.td_redeem_promo($1, 'X1', 'premium', 30) r`, [U(40)]));
  check("PR #8: promo refused after a Stripe payment (plan_active)", !r.r.ok && r.r.code === "plan_active", JSON.stringify(r.r));
  // And the other way: a promo, then a payment replaces it.
  await db.query(`insert into auth.users (id) values ($1)`, [U(42)]);
  const pr = await as("service_role", null, () => one(`select public.td_redeem_promo($1, 'X2', 'premium', 30) r`, [U(42)]));
  const pay = await apply(U(42), "pro", "cus_42");
  const p42 = await prof(U(42));
  check("PR #8: payment after a promo replaces it and clears the end date",
    pr.r.ok && pay.applied && p42.subscription_tier === "pro" && p42.subscription_expires_at === null, JSON.stringify([pr.r, p42]));
} else {
  console.log("(PR #8's td_redeem_promo not present; promo-function checks skipped)");
}

// ── Run order B: this script first, then PR #8, then this script again ─
const db2 = new PGlite();
await db2.exec(`
create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;
create schema auth;
create table auth.users (id uuid primary key, email text, created_at timestamptz default now());
create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
create table public.profiles (id uuid primary key references auth.users(id), subscription_tier text default 'free', created_at timestamptz default now());
create table public.watchlists (user_id uuid references auth.users(id), ticker text not null, primary key (user_id, ticker));
create table public.report_generations (id uuid primary key default gen_random_uuid(), user_id uuid references auth.users(id),
  ticker text not null, report_type text not null, status text default 'queued', created_at timestamptz default now());
insert into auth.users (id) values ('${U(1)}');
`);
const orderB = [];
for (const [label, sql] of [["this script", PR17], ["PR #8", PR8], ["this script again", PR17]]) {
  try {
    await db2.exec(sql);
    orderB.push(label + " ok");
  } catch (e) {
    orderB.push(label + " FAILED: " + e.message);
  }
}
check("either order works (this script, PR #8, this script)", orderB.every((s) => s.endsWith(" ok")), orderB.join("; "));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
