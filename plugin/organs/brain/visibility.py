"""远端输入的来源筛选；调用者传真实会话标识，不采信模型提供的身份。"""

PROFILE_FIELDS = frozenset(('likes', 'dislikes', 'views', 'self_description', 'stance', 'sleep_notice'))


def visible(row, *, session_id=None):
    if row.get('local_only') is True or row.get('scope') == 'local':
        return False
    source = row.get('session_id')
    if row.get('scene') == 'private' or str(source or '').startswith('qq:private:'):
        return bool(session_id and source == session_id)
    if row.get('audience') == '公开' or row.get('scope') == 'public':
        return True
    return bool(session_id and source == session_id)
