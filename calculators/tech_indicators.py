import pandas as pd
import numpy as np

class TechIndicators:
    @staticmethod
    def calculate_all(stock_daily_data, index_daily_data=None):
        """
        stock_daily_data: KIS API에서 받은 일별 데이터 (list of dict)
        index_daily_data: KIS API에서 받은 지수 일별 데이터 (list of dict)
        """
        if not stock_daily_data or 'output' not in stock_daily_data:
            return {}

        df = pd.DataFrame(stock_daily_data['output'])
        df['close'] = df['stck_clpr'].astype(float)
        df['volume'] = df['acml_vol'].astype(float)
        # 데이터가 역순(최신이 0번)인 경우가 많으므로 순차적으로 정렬
        df = df.iloc[::-1].reset_index(drop=True)

        results = {}

        # 1. 이격도 (Divergence)
        curr_price = df['close'].iloc[-1]
        for ma_period in [5, 20, 60, 120]:
            ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
            if ma_val > 0:
                results[f'div_{ma_period}'] = round((curr_price / ma_val) * 100, 2)
            else:
                results[f'div_{ma_period}'] = None

        # 2. 복기 수익률 (Review)
        periods = {'prev_1d': 1, 'prev_5d': 5, 'prev_20d': 20}
        for label, p in periods.items():
            if len(df) > p:
                prev_price = df['close'].iloc[-(p+1)]
                results[f'return_{label}'] = round(((curr_price / prev_price) - 1) * 100, 2)

        # 3. 상대강도 (Relative Strength)
        if index_daily_data and ('output2' in index_daily_data or 'output' in index_daily_data):
            idx_list = index_daily_data.get('output2', index_daily_data.get('output', []))
            if idx_list:
                idx_df = pd.DataFrame(idx_list)
                # 지수 TR에 따라 필드명이 다를 수 있음 (nmix: 지수)
                idx_cols = idx_df.columns
                price_col = 'bstp_nmix_prpr' if 'bstp_nmix_prpr' in idx_cols else 'stck_clpr'
                
                idx_df['close'] = idx_df[price_col].astype(float)
                idx_df = idx_df.iloc[::-1].reset_index(drop=True)
                
                # 최근 20일간의 수익률 비교
                if len(df) >= 20 and len(idx_df) >= 20:
                    stock_ret = (df['close'].iloc[-1] / df['close'].iloc[-20]) - 1
                    index_ret = (idx_df['close'].iloc[-1] / idx_df['close'].iloc[-20]) - 1
                    results['rs_score_20d'] = round((stock_ret - index_ret) * 100, 2)

        # 4. 매물대 분석 (Volume Profile - Simple)
        # 최근 1년(또는 가용 데이터)의 가격 구간별 거래량 합계
        price_min = df['close'].min()
        price_max = df['close'].max()
        if price_max > price_min:
            bins = np.linspace(price_min, price_max, 10)
            df['bin'] = pd.cut(df['close'], bins=bins)
            volume_profile = df.groupby('bin', observed=True)['volume'].sum()
            max_vol_bin = volume_profile.idxmax()
            
            results['main_support_zone'] = f"{int(max_vol_bin.left)} ~ {int(max_vol_bin.right)}"
            # 현재가가 최대 매물대 위에 있는지 여부 (bool_ -> bool 변환)
            results['above_main_volume_profile'] = bool(curr_price > max_vol_bin.right)

        # 5. 돌파 여부 (Breakout)
        # 최근 60일 신고가 근접 여부
        if len(df) >= 60:
            high_60 = df['close'].iloc[-60:-1].max()
            results['is_60d_high_breakout'] = bool(curr_price > high_60)
            results['dist_from_60d_high'] = round(((curr_price / high_60) - 1) * 100, 2)


        return results
