"""
5단계(탐색적): 리뷰 평판 점수를 시나리오 변수로 쓰는 동(洞) 단위 System Dynamics 모형

입력  data/raw/서울시 성동구 일반음식점 인허가 정보.csv     연도별 진입·폐업·영업 점포 수 (2015~2025)
      data/final_dataset_with_reviews.csv                동별 기준 시점 100m 음식점 수(BIZ)
      outputs/tables/cox_radius_models.csv               Cox 추정 BIZ 계수 (모형 B, 100m)
      data/interim/restaurant_review_scores.csv          리뷰 품질 점수 (2026-10 수집, 영업 점포 2,119곳)
출력  outputs/sd/*.csv, outputs/sd/*.png, outputs/sd/sd_summary.md

실행  python code/05_sd_review_scenarios.py

모형 (상권 단위 k = 성동구 17개 법정동, 연 단위 흐름, Δt = 0.25년)
  S_k(t+Δt) = S_k(t) + [E_k(t) − D_k(t)]·Δt                     영업 점포 수 (stock)
  D_k(t)    = S_k(t) · d_k · exp(β_BIZ·ΔBIZ_k(t)) · exp(−γ·ΔR_k(t))  폐업 (flow)
  E_k(t)    = e_k · exp(η·ΔR_k(t))                                신규 진입 (flow)
  ΔBIZ_k(t) = BIZ_k^ref · (S_k(t)/S_k^ref − 1)                    밀도 변화 → 폐업률 (Cox 집적 효과 피드백)
  R_k(t+Δt) = R_k(t) + Δt·(R_k^target − R_k(t))/τ                 평판 조정 (지연)
  ΔR_k(t)   = coverage_k · (R_k(t) − R_k^base)                    점수 관측 점포 비율만큼만 상권 전체에 반영

  d_k, e_k  : 동별 기준 폐업률·진입 수 (검증: 2015~2020 평균, 예측: 2021~2025 평균)
  β_BIZ     : Cox 모형 B 100m의 Adjacent_BIZ 계수 (10곳당 HR 0.921 → 1곳당 ln(0.921)/10)
  R_k       : 동 평균 리뷰 평판 z_i = (Q_i − 50)/50, Q_i = review_quality_score_0_100 (점수 있는 점포만)
  η, γ      : 리뷰 → 진입(옵션 A), 리뷰 → 폐업(옵션 B) 연결 강도. 추정값이 아닌 시나리오 가정 (0, 0.1, 0.3)

해석상 주의
  - 리뷰 점수는 2026년 영업 점포에서만 관측되므로 과거 검증(2021~2025)에는 넣지 않는다.
  - η, γ는 실증 계수가 아니다. 결과는 "평판이 이만큼 진입·폐업과 연결된다고 가정하면"의 민감도 분석이다.
"""
from pathlib import Path
import re
import unicodedata
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DATA_PATH = ROOT / "data" / "final_dataset_with_reviews.csv"
COX_PATH = ROOT / "outputs" / "tables" / "cox_radius_models.csv"
REVIEW_PATH = ROOT / "data" / "interim" / "restaurant_review_scores.csv"
OUT_DIR = ROOT / "outputs" / "sd"

YEARS = range(2015, 2026)
CALIB_YEARS = range(2015, 2021)        # 검증용 모수 추정
VALID_YEARS = range(2021, 2026)        # 과거 재현 검증
RECENT_YEARS = range(2021, 2026)       # 예측용 모수 추정
FORECAST_YEARS = range(2026, 2031)     # 5년 예측
DT = 0.25
TAU_REVIEW = 2.0                       # 평판이 목표치에 수렴하는 시간(년), 가정값


# ---------------------------------------------------------------- 1. 동별 연도 시계열
def find_license_file() -> Path:
    hits = [p for p in RAW_DIR.iterdir() if "인허가" in unicodedata.normalize("NFC", p.name)]
    assert len(hits) == 1, hits
    return hits[0]


def extract_dong(jibun, road):
    if isinstance(jibun, str):
        m = re.search(r"성동구\s+([가-힣]+(?:동\d*가|동|가))", jibun)
        if m:
            return m.group(1)
    if isinstance(road, str):
        m = re.search(r"\(([가-힣]+(?:동\d*가|동|가))", road)
        if m:
            return m.group(1)
    return None


def dong_history() -> pd.DataFrame:
    """동 × 연도: 신규 진입, 폐업, 연말 영업 점포 수"""
    path = find_license_file()
    lic = pd.read_csv(path, encoding="cp949", low_memory=False) if path.suffix != ".zip" else \
        pd.read_csv(path, encoding="cp949", low_memory=False, compression="zip")
    lic["open"] = pd.to_datetime(lic["인허가일자"], errors="coerce")
    lic["close"] = pd.to_datetime(lic["폐업일자"], errors="coerce")
    lic["dong"] = [extract_dong(a, b) for a, b in zip(lic["지번주소"], lic["도로명주소"])]
    lic = lic[lic["dong"].notna() & lic["open"].notna()]
    rows = []
    for y in [YEARS[0] - 1, *YEARS]:
        end = pd.Timestamp(f"{y}-12-31")
        alive = (lic["open"] <= end) & (lic["close"].isna() | (lic["close"] > end))
        g = lic.assign(entry=lic["open"].dt.year == y, exit=lic["close"].dt.year == y, stock=alive)
        agg = g.groupby("dong")[["entry", "exit", "stock"]].sum().reset_index().assign(year=y)
        rows.append(agg)
    hist = pd.concat(rows, ignore_index=True).sort_values(["dong", "year"])
    hist["stock_start"] = hist.groupby("dong")["stock"].shift(1)
    hist["exit_rate"] = hist["exit"] / hist["stock_start"]
    return hist[hist["year"].isin(YEARS)].reset_index(drop=True)


def params(hist: pd.DataFrame, years) -> pd.DataFrame:
    h = hist[hist["year"].isin(years)]
    return h.groupby("dong").agg(e=("entry", "mean"), d=("exit_rate", "mean")).reset_index()


# ---------------------------------------------------------------- 2. Cox 계수와 동별 밀도, 리뷰 평판
def beta_biz() -> float:
    r = pd.read_csv(COX_PATH)
    hr10 = r[(r["model"] == "B") & (r["variable"] == "adjacent_biz_100m")]["hr"].iloc[0]
    return np.log(hr10) / 10


def dong_biz() -> pd.Series:
    df = pd.read_csv(DATA_PATH)
    return df.groupby("dong")["adjacent_biz_100m"].mean()


def dong_review(stock_now: pd.Series) -> pd.DataFrame:
    """동 평균 평판 R_k, 관측 비율 coverage_k = 점수 보유 점포 / 2025년 말 전체 영업 점포
    (리뷰는 2021년 코호트 생존 점포에서만 수집했으므로, 2021년 이후 진입 점포는 미관측으로 본다)"""
    sc = pd.read_csv(REVIEW_PATH, dtype={"restaurant_id": str})
    df = pd.read_csv(DATA_PATH, dtype={"restaurant_id": str})[["restaurant_id", "dong", "event"]]
    m = df[df["event"] == 0].merge(sc[["restaurant_id", "review_score_available", "review_quality_score_0_100"]],
                                   on="restaurant_id", how="left")
    m["scored"] = m["review_score_available"].fillna(False).astype(bool) & m["review_quality_score_0_100"].notna()
    m["z"] = (m["review_quality_score_0_100"] - 50) / 50
    out = m.groupby("dong").agg(cohort_open_stores=("restaurant_id", "size"), scored_stores=("scored", "sum"),
                                R_base=("z", "mean"), Q_mean=("review_quality_score_0_100", "mean")).reset_index()
    out["all_open_stores_2025"] = out["dong"].map(stock_now)
    out["coverage"] = out["scored_stores"] / out["all_open_stores_2025"]
    return out, m


# ---------------------------------------------------------------- 3. SD 시뮬레이션
def simulate(S0, e, d, biz_ref, beta, years, R_base=None, R_target=None, coverage=None,
             eta=0.0, gamma=0.0, entry_mult=1.0):
    """동 벡터 단위 Euler 적분. 반환: 연도별 연말 점포 수, 연간 진입·폐업"""
    S = S0.astype(float).copy()
    S_ref = S0.astype(float).copy()
    n = len(S)
    R = np.zeros(n) if R_base is None else R_base.copy()
    Rb = R.copy()
    Rt = R.copy() if R_target is None else R_target
    cov = np.zeros(n) if coverage is None else coverage
    steps = int(round(1 / DT))
    rows = []
    for y in years:
        ent = np.zeros(n); ext = np.zeros(n)
        for _ in range(steps):
            dR = cov * (R - Rb)
            dbiz = biz_ref * (S / S_ref - 1)
            D = S * d * np.exp(beta * dbiz) * np.exp(-gamma * dR)
            E = e * np.exp(eta * dR) * entry_mult
            S = np.maximum(S + (E - D) * DT, 0)
            R = R + DT * (Rt - R) / TAU_REVIEW
            ent += E * DT; ext += D * DT
        rows.append({"year": y, "stock": S.copy(), "entry": ent, "exit": ext})
    return rows


def to_frame(rows, dongs, scenario):
    out = []
    for r in rows:
        out.append(pd.DataFrame({"scenario": scenario, "year": r["year"], "dong": dongs,
                                 "stock": r["stock"], "entry": r["entry"], "exit": r["exit"]}))
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------- 4. 그림
def set_font():
    for path in font_manager.findSystemFonts():
        if any(k in path for k in ["AppleSDGothicNeo", "NotoSansCJK", "NanumGothic", "malgun"]):
            font_manager.fontManager.addfont(path)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
            break
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.unicode_minus": False})


def plot_validation(hist_tot, valid_tot, mape):
    fig, ax = plt.subplots(figsize=(6.5, 3.2), dpi=200)
    ax.plot(hist_tot["year"], hist_tot["stock"], "o-", color="#444", lw=1.4, ms=3.5, label="실제 (인허가)")
    ax.plot(valid_tot["year"], valid_tot["stock"], "s--", color="#2a5d9f", lw=1.4, ms=3.5,
            label="SD 재현 (2015~2020 모수)")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.axvline(2020.5, color="#aaa", lw=0.7, ls=":")
    y0 = ax.get_ylim()[0]
    ax.text(2017.5, y0 + 40, "보정 구간 (2015~2020)", ha="center", va="bottom", color="#777", fontsize=8)
    ax.text(2022.9, y0 + 40, f"검증 구간 (MAPE {mape:.1f}%)", ha="center", va="bottom", color="#777", fontsize=8)
    ax.set(xlabel="연도", ylabel="연말 영업 점포 수", title="SD 기준 모형의 과거 재현 (성동구 전체)")
    ax.grid(axis="y", color="#e5e5e5", lw=0.5)
    fig.savefig(OUT_DIR / "fig_sd_validation.png", bbox_inches="tight")
    plt.close(fig)


def plot_scenarios(summ, base_2025, base_2030):
    """2030년 점포 수의 기준 시나리오 대비 차이 (막대)"""
    d = summ[summ["scenario"] != "S0"].iloc[::-1]
    fig, ax = plt.subplots(figsize=(6.5, 3.0), dpi=200)
    colors = ["#2a5d9f" if v >= 0 else "#b5523b" for v in d["diff_vs_base"]]
    ax.barh(d["label"], d["diff_vs_base"], color=colors, height=0.6)
    for y, (v, pct) in enumerate(zip(d["diff_vs_base"], d["diff_pct"])):
        ax.text(v + (1 if v >= 0 else -1), y, f"{v:+.0f}곳 ({pct:+.2f}%)", va="center",
                ha="left" if v >= 0 else "right", fontsize=7.5)
    ax.axvline(0, color="#666", lw=0.7)
    lim = d["diff_vs_base"].abs().max() * 1.6
    ax.set_xlim(-lim, lim)
    ax.set(xlabel="2030년 영업 점포 수, 기준 시나리오 대비 차이(곳)",
           title=f"리뷰 평판 시나리오별 2030년 음식점 수 (기준 {base_2030:,.0f}곳, 2025년 말 {base_2025:,.0f}곳)")
    ax.tick_params(axis="y", length=0, labelsize=7.5)
    ax.grid(axis="x", color="#e5e5e5", lw=0.5)
    ax.set_axisbelow(True)
    fig.savefig(OUT_DIR / "fig_sd_review_scenarios.png", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------- 실행
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    set_font()

    hist = dong_history()
    hist.to_csv(OUT_DIR / "dong_history_2015_2025.csv", index=False, encoding="utf-8-sig")
    dongs = sorted(hist["dong"].unique())
    stock = hist.pivot(index="dong", columns="year", values="stock").loc[dongs]
    beta = beta_biz()
    biz2021 = dong_biz().reindex(dongs).to_numpy()
    print(f"동 {len(dongs)}개, β_BIZ = {beta:.5f} (1곳당), 2025년 말 영업 점포 {stock[2025].sum():,}곳")

    # --- 4-1. 과거 재현 검증: 2015~2020 모수로 2021~2025 재현 (리뷰 없음)
    p = params(hist, CALIB_YEARS).set_index("dong").loc[dongs]
    biz_ref = biz2021 * stock[2020].to_numpy() / stock[2021].to_numpy()
    rows = simulate(stock[2020].to_numpy(), p["e"].to_numpy(), p["d"].to_numpy(), biz_ref, beta, VALID_YEARS)
    valid = to_frame(rows, dongs, "validation")
    actual = hist[hist["year"].isin(VALID_YEARS)][["dong", "year", "stock", "entry", "exit"]]
    cmp_ = valid.merge(actual, on=["dong", "year"], suffixes=("_sim", "_actual"))
    cmp_.to_csv(OUT_DIR / "validation_2021_2025_by_dong.csv", index=False, encoding="utf-8-sig")
    tot = cmp_.groupby("year")[["stock_sim", "stock_actual"]].sum()
    mape_total = (abs(tot.stock_sim - tot.stock_actual) / tot.stock_actual).mean() * 100
    end = cmp_[cmp_["year"] == 2025]
    mape_dong = (abs(end.stock_sim - end.stock_actual) / end.stock_actual).mean() * 100
    print(f"검증: 성동구 전체 MAPE {mape_total:.1f}%, 2025년 동별 MAPE {mape_dong:.1f}%")
    hist_tot = hist.groupby("year")["stock"].sum().reset_index()
    valid_tot = pd.concat([pd.DataFrame({"year": [2020], "stock": [stock[2020].sum()]}),
                           tot.reset_index().rename(columns={"stock_sim": "stock"})[["year", "stock"]]])
    plot_validation(hist_tot, valid_tot, mape_total)

    # --- 4-2. 리뷰 평판 입력
    rev, store = dong_review(stock[2025])
    rev = rev.set_index("dong").reindex(dongs)
    rev.to_csv(OUT_DIR / "review_by_dong.csv", encoding="utf-8-sig")
    R_base = rev["R_base"].fillna(rev["R_base"].mean()).to_numpy()
    cov = rev["coverage"].fillna(0).to_numpy()
    print(f"리뷰 점수 보유 점포 {int(rev.scored_stores.sum()):,}곳 / 2025년 말 전체 영업 점포 {int(rev.all_open_stores_2025.sum()):,}곳 "
          f"(coverage {rev.scored_stores.sum() / rev.all_open_stores_2025.sum():.1%})")

    def target(delta_fn):
        """점포별 점수 변화 → 동 평균 평판 목표치"""
        s = store[store["scored"]].copy()
        s["Q_new"] = np.minimum(s["review_quality_score_0_100"] + delta_fn(s["review_quality_score_0_100"]), 100)
        Rt = ((s["Q_new"] - 50) / 50).groupby(s["dong"]).mean().reindex(dongs)
        return Rt.fillna(pd.Series(R_base, index=dongs)).to_numpy()

    q25 = store.loc[store["scored"], "review_quality_score_0_100"].quantile(0.25)
    up_all = target(lambda q: 5.0)                                   # 모든 점포 +5점
    up_low = target(lambda q: np.where(q < q25, 10.0, 0.0))          # 하위 25% 점포만 +10점 (경영지원)
    down_all = target(lambda q: -5.0)                                # 평판 악화 -5점

    # --- 4-3. 2026~2030 예측
    pr = params(hist, RECENT_YEARS).set_index("dong").loc[dongs]
    S0 = stock[2025].to_numpy()
    biz_ref25 = biz2021 * S0 / stock[2021].to_numpy()
    scenarios = {
        "S0": ("기준 (리뷰 효과 없음)", None, 0.0, 0.0),
        "A1": ("A: 전 점포 +5점, 진입 η=0.1", up_all, 0.1, 0.0),
        "A3": ("A: 전 점포 +5점, 진입 η=0.3", up_all, 0.3, 0.0),
        "B1": ("B: 전 점포 +5점, 폐업 γ=0.1", up_all, 0.0, 0.1),
        "B3": ("B: 전 점포 +5점, 폐업 γ=0.3", up_all, 0.0, 0.3),
        "AB3": ("A+B: 전 점포 +5점, η=γ=0.3", up_all, 0.3, 0.3),
        "LOW": ("A+B: 하위 25%만 +10점, η=γ=0.3", up_low, 0.3, 0.3),
        "DOWN": ("A+B: 전 점포 −5점, η=γ=0.3", down_all, 0.3, 0.3),
    }
    frames = []
    for key, (label, Rt, eta, gamma) in scenarios.items():
        rows = simulate(S0, pr["e"].to_numpy(), pr["d"].to_numpy(), biz_ref25, beta, FORECAST_YEARS,
                        R_base=R_base, R_target=Rt, coverage=cov, eta=eta, gamma=gamma)
        frames.append(to_frame(rows, dongs, key).assign(label=label))
    res = pd.concat(frames, ignore_index=True)
    res.to_csv(OUT_DIR / "scenario_results_by_dong_2026_2030.csv", index=False, encoding="utf-8-sig")
    tot = res.groupby(["scenario", "label", "year"])[["stock", "entry", "exit"]].sum().reset_index()
    tot.to_csv(OUT_DIR / "scenario_results_total_2026_2030.csv", index=False, encoding="utf-8-sig")

    # --- 요약
    base30 = tot[(tot.scenario == "S0") & (tot.year == 2030)]["stock"].iloc[0]
    summ = []
    for key, (label, *_ ) in scenarios.items():
        t = tot[tot.scenario == key]
        s30 = t[t.year == 2030]["stock"].iloc[0]
        summ.append({"scenario": key, "label": label, "stock_2030": round(s30, 1),
                     "diff_vs_base": round(s30 - base30, 1), "diff_pct": round((s30 / base30 - 1) * 100, 2),
                     "entries_5y": round(t["entry"].sum(), 1), "exits_5y": round(t["exit"].sum(), 1)})
    summ = pd.DataFrame(summ)
    summ.to_csv(OUT_DIR / "scenario_summary_2030.csv", index=False, encoding="utf-8-sig")
    plot_scenarios(summ, S0.sum(), base30)
    print(summ.to_string(index=False))

    lines = [
        "# SD 리뷰 평판 시나리오 결과 요약 (탐색적)", "",
        f"- 단위: 성동구 17개 법정동, 2025년 말 영업 점포 {S0.sum():,.0f}곳에서 2026~2030년 예측 (Δt = {DT}년)",
        f"- 폐업 피드백: Cox 모형 B 100m Adjacent_BIZ 계수 β = {beta:.5f}/곳 (10곳당 HR {np.exp(beta * 10):.3f})",
        f"- 과거 재현 검증 (2015~2020 모수 → 2021~2025): 성동구 전체 MAPE {mape_total:.1f}%, 2025년 동별 MAPE {mape_dong:.1f}%",
        f"- 리뷰 점수 보유 점포 {int(rev.scored_stores.sum()):,}곳 / 2025년 말 전체 영업 점포 {int(rev.all_open_stores_2025.sum()):,}곳"
        f" (coverage {rev.scored_stores.sum() / rev.all_open_stores_2025.sum():.1%}); "
        f"평판 조정 시간 τ = {TAU_REVIEW}년; 하위 25% 기준 Q < {q25:.1f}점",
        "- η(리뷰→진입), γ(리뷰→폐업)는 추정값이 아닌 가정값이며, 결과는 민감도 분석이다.", "",
        "| 시나리오 | 2030년 점포 수 | 기준 대비 | 5년 진입 | 5년 폐업 |", "|---|---|---|---|---|",
        *[f"| {r.label} | {r.stock_2030:,.0f} | {r.diff_vs_base:+.0f} ({r.diff_pct:+.2f}%) | {r.entries_5y:,.0f} | {r.exits_5y:,.0f} |"
          for r in summ.itertuples()], ""]
    (OUT_DIR / "sd_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("저장:", OUT_DIR.relative_to(ROOT))


if __name__ == "__main__":
    main()
