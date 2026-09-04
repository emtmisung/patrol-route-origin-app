# 파세루 오리진 (FireSafe Route Origin) — 실동 버전

성주소방서 폭염순찰(마을회관) 등 다수 거점 순찰 노선을, 실제 도로거리·소요시간 기준으로 자동 편성하는 Streamlit 앱입니다.
- 주소 CSV/Excel 업로드 → NCP Geocoding으로 좌표 변환
- 최근접 이웃(NN) 알고리즘으로 노선 편성 (구간별 제한 / 목표 왕복시간 두 가지 모드)
- 장거리 대상 자동 분리
- 노선별 지도 시각화 (실도로 경로선)
- 지점별 카카오맵 길안내 링크 제공

## 1. 로컬에서 실행하기

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# secrets.toml을 열어 NCP_CLIENT_ID / NCP_CLIENT_SECRET 값을 입력
streamlit run app.py
```

## 2. 핸드폰에서 접속 가능한 공개 링크로 배포하기 (Streamlit Community Cloud, 무료)

1. **GitHub 저장소 만들기**
   - https://github.com 에서 새 저장소(Repository) 생성 (Private로 설정 권장)
   - 이 폴더의 파일들을 업로드 (`.streamlit/secrets.toml`은 `.gitignore`에 의해 제외되므로 절대 올라가지 않습니다 — `secrets.toml.example`만 올라갑니다)

2. **Streamlit Community Cloud 배포**
   - https://share.streamlit.io 접속 → GitHub 계정으로 로그인
   - "New app" → 방금 만든 저장소 선택, Main file path에 `app.py` 입력
   - **Advanced settings → Secrets**에 아래 내용을 붙여넣기:
     ```toml
     NCP_CLIENT_ID = "실제 Client ID"
     NCP_CLIENT_SECRET = "실제 Client Secret"
     ```
   - Deploy 클릭 → 몇 분 내 `https://xxxx.streamlit.app` 형태의 공개(비공개 설정 가능) URL 생성
   - 이 URL을 핸드폰 브라우저로 열면 바로 실제 앱이 구동됩니다.

> API 키는 GitHub 코드에는 절대 포함되지 않고, Streamlit Cloud의 Secrets 저장소에만 저장되어 브라우저로 노출되지 않습니다.

## 3. 심사용 버전으로 전환할 때

실동이 원하는 대로 확인되면, 심사용으로는:
- 샘플 데이터(사전 검증된 성주군 데이터)만 기본 표시되도록 업로드 UI를 숨기거나 데모 모드 배지를 추가
- 필요 시 `README`/앱 상단에 "제1회 경북소방 AI 공모전 출품작" 문구 추가
- 같은 코드베이스이므로 브랜치만 나누어 관리하면 됩니다 (`main`=실사용, `submission`=심사용).

## 참고

- NCP API 호출은 이 개발 환경(샌드박스)에서는 조직 네트워크 정책상 직접 테스트가 막혀 있어, Streamlit Cloud 배포 후 실제 인터넷 환경에서 처음 검증됩니다.
- 노선 편성(다음 방문지 선택), 장거리 분리 판정, 최종 구간 확정까지 전 과정이 NCP Directions5 실제 도로거리/시간 기준입니다. 직선거리는 API 호출이 실패했을 때만 비상 대체값으로 쓰입니다.
- 이 방식은 지점 수가 많아질수록 API 호출 횟수가 O(n²)로 늘어납니다(30개소 기준 약 400~500회 호출). 217개소 전체를 한 번에 돌릴 경우 호출량이 크게 늘어나니, 필요하면 읍·면 단위로 나눠 실행하거나 캐시(`st.cache_data`)를 활용해 재실행 시간을 줄이세요.
