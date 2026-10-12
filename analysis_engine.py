"""源码结构分析：AST 结构与真实运行时调用关系分开呈现。"""
import ast

MAX_CODE = 30_000


def validate_code(code):
    if not isinstance(code, str) or not code.strip():
        raise ValueError('请先输入 Python 代码。')
    if len(code) > MAX_CODE:
        raise ValueError('代码长度不能超过 30000 个字符。')
    return code


def analyze(code):
    validate_code(code)
    root = ast.parse(code, filename='<pyscope>')
    nodes = list(ast.walk(root))
    counts = {
        'lines': len(code.splitlines()),
        'functions': sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in nodes),
        'classes': sum(isinstance(n, ast.ClassDef) for n in nodes),
        'branches': sum(isinstance(n, (ast.If, ast.IfExp, ast.Match)) for n in nodes),
        'loops': sum(isinstance(n, (ast.For, ast.While, ast.AsyncFor)) for n in nodes),
    }
    names = {'Module': '程序', 'FunctionDef': '函数', 'ClassDef': '类', 'Assign': '赋值',
             'AnnAssign': '带类型的赋值', 'AugAssign': '增量赋值', 'For': 'for 循环',
             'While': 'while 循环', 'If': '条件分支', 'Return': '返回', 'Call': '函数调用',
             'Expr': '表达式', 'Try': '异常处理', 'Raise': '抛出异常', 'Import': '导入',
             'ImportFrom': '导入', 'With': '上下文', 'Compare': '比较'}
    remaining = [350]

    def build(node, depth=0):
        remaining[0] -= 1
        kind = type(node).__name__
        label = names.get(kind, kind)
        if hasattr(node, 'name'):
            label += ': ' + node.name
        elif isinstance(node, ast.Name):
            label += ': ' + node.id
        elif isinstance(node, ast.Constant):
            label += ': ' + repr(node.value)[:70]
        elif isinstance(node, ast.Call):
            label += ': ' + ast.unparse(node.func)[:70]
        item = {'label': label, 'line': getattr(node, 'lineno', 0), 'children': []}
        if depth < 12 and remaining[0] > 0:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.expr_context, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)):
                    continue
                if remaining[0] <= 0:
                    break
                item['children'].append(build(child, depth + 1))
        return item

    tree = build(root)
    warnings = []
    if any(isinstance(n, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)) for n in nodes):
        warnings.append('此程序含异步或生成器语法，超出本版本完整调试可视化的支持范围。')
    if remaining[0] <= 0:
        warnings.append('AST 展示已限制为 350 个节点，避免大程序阻塞界面。')
    return {'tree': tree, 'counts': counts,
            'functions': [{'name': n.name, 'line': n.lineno} for n in nodes
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
            'warnings': warnings}
