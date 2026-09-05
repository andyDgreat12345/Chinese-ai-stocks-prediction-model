fetch_china_close: ETF 510300 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 159915 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 512480 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 159930 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 512800 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 512170 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 159928 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 512880 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 512660 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
fetch_china_close: ETF 515030 via eastmoney failed: ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))
China sector-ETF code verification

  [ok  ] broad        510300   3451 bars
  [ok  ] brokers      512880   2428 bars
  [ok  ] consumer     159928   3133 bars
  [ok  ] defense      512660   2428 bars
  [ok  ] energy       159930   3129 bars
  [ok  ] financials   512800   2188 bars
  [ok  ] growth       159915   3559 bars
  [ok  ] healthcare   512170   1734 bars
  [ok  ] newenergy    515030   1561 bars
  [ok  ] semis        512480   1737 bars

  10/10 codes resolved.
China bar quality — moves beyond each instrument's daily price limit
  (main board ±10%, ChiNext/STAR ±20%)

  5 implausible bar(s) — these are corporate actions or bad
  prints, and both the backtest and the simulator score them as real:

    2021-06-25  159928   consumer      -74.47%  (limit ±11%)
    2021-02-25  512170   healthcare    -68.10%  (limit ±11%)
    2026-07-03  512480   semis         -50.70%  (limit ±11%)
    2025-07-07  512800   financials    -49.69%  (limit ±11%)
    2021-03-29  512480   semis         -48.90%  (limit ±11%)
