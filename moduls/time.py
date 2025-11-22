import arrow

async def get_this_weekday(reference_date:str, reference_color:str, date:str = arrow.now().format("DD.MM.YYYY")) -> list:
    """"
    Получение текущего дня недели и цевета недели на основе даты и цвета недели заданных в global.json.
    """
    
    try:
        day = arrow.get(date, "DD.MM.YYYY", tzinfo='local')
    except:
        raise ValueError('Неверный формат даты. Используйте ДД.ММ.ГГГГ')

    weekdays = ['ПН', 'ВТ', 'СР', 'ЧТ', 'ПТ', 'СБ', 'ВС']
    weekday = weekdays[day.weekday()]

    if ((day.floor('week') - arrow.get(reference_date, "DD.MM.YYYY", tzinfo='local').floor('week')).days // 7) % 2 == 0:
        color = reference_color
    else:
        color = 'Зелёная неделя' if reference_color == 'Белая неделя' else 'Белая неделя'

    return [weekday, color]

async def get_next_previous(date:str = arrow.now(), direction:str = '') -> str:
    """
    Получение следующей или предыдущей даты на основе переданной даты.
    """
    
    if direction == 'extra_next':
        finall_date = arrow.get(date, "DD.MM.YYYY", tzinfo='local').shift(weeks=+1).format("DD.MM.YYYY")
    elif direction == 'next':
        finall_date = arrow.get(date, "DD.MM.YYYY", tzinfo='local').shift(days=+1).format("DD.MM.YYYY")
    elif direction == 'previous':
        finall_date = arrow.get(date, "DD.MM.YYYY", tzinfo='local').shift(days=-1).format("DD.MM.YYYY")
    elif direction == 'extra_previous':
        finall_date = arrow.get(date, "DD.MM.YYYY", tzinfo='local').shift(weeks=-1).format("DD.MM.YYYY")
    else:
        finall_date = ''
    
    return finall_date
