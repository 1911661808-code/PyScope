"""可解释、可重现的课程演示示例。"""
EXAMPLES = [
    {'id': 'bubble', 'title': '冒泡排序', 'category': '列表与循环',
     'description': '观察相邻交换、循环下标和列表状态。',
     'code': '''def bubble_sort(values):
    numbers = values.copy()
    for end in range(len(numbers) - 1, 0, -1):
        for i in range(end):
            if numbers[i] > numbers[i + 1]:
                numbers[i], numbers[i + 1] = numbers[i + 1], numbers[i]
    return numbers

data = [7, 3, 9, 2, 5]
result = bubble_sort(data)
print("原始列表:", data)
print("排序结果:", result)
'''},
    {'id': 'recursion', 'title': '递归阶乘', 'category': '调用栈',
     'description': '进入每一层递归，查看独立的局部变量与返回值。',
     'code': '''def factorial(n):
    if n <= 1:
        return 1
    child = factorial(n - 1)
    answer = n * child
    return answer

number = 5
result = factorial(number)
print(number, "的阶乘是", result)
'''},
    {'id': 'alias', 'title': '共享引用与浅拷贝', 'category': '对象关系',
     'description': '同一个对象为什么会被多个变量共同修改？',
     'code': '''original = [[1, 2], [3, 4]]
alias = original
shallow = original.copy()
original[0].append(99)
print("original:", original)
print("alias:", alias)
print("shallow:", shallow)
print("alias 是 original:", alias is original)
print("共享内层列表:", shallow[0] is original[0])
'''},
    {'id': 'linked', 'title': '链表与循环引用', 'category': '对象关系',
     'description': '自定义对象的 next 引用组成一条环。',
     'code': '''class Node:
    def __init__(self, value):
        self.value = value
        self.next = None

first = Node("A")
second = Node("B")
third = Node("C")
first.next = second
second.next = third
third.next = first
current = first
for step in range(4):
    print(current.value)
    current = current.next
'''},
    {'id': 'exception', 'title': '定位除零异常', 'category': '异常调试',
     'description': '保留发生异常时的栈帧、变量和错误行。',
     'code': '''def average(values):
    total = sum(values)
    count = len(values)
    return total / count

scores = []
result = average(scores)
print(result)
'''},
    {'id': 'profile_slow', 'title': '性能对比：重复递归', 'category': '性能实验',
     'description': '直接运行性能分析，观察重复调用。',
     'code': '''def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

result = fibonacci(22)
print("结果:", result)
'''},
    {'id': 'profile_fast', 'title': '性能对比：记忆化递归', 'category': '性能实验',
     'description': '与重复递归比较调用次数；计时会受机器环境影响。',
     'code': '''cache = {}

def fibonacci(n):
    if n in cache:
        return cache[n]
    if n < 2:
        result = n
    else:
        result = fibonacci(n - 1) + fibonacci(n - 2)
    cache[n] = result
    return result

result = fibonacci(22)
print("结果:", result)
'''},
]
