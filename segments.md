K-line segment accuracy — where the edge actually is

  segment                n      hit   edge_t  capturable entering at the open
  --------------------------------------------------------------
  gap                 7151   71.81%   +18.44  NO
  body                7988   49.41%    -0.53  yes
  close_to_close      8026   57.23%    +6.47  NO

  Read the 'capturable' column before the hit rate. The gap runs from the
  previous close to this open, and the US session driving it trades inside
  that window — owning the gap means entering before the US session the
  signal comes from. A high gap hit rate is information that arrives too
  late to act on, not an edge.

  The body is what a position entered at the open actually earns.

  Not investment advice.
