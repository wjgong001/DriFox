# -*- coding: utf-8 -*-
"""
工具执行器模块 - 统一处理各种工具调用
"""
import threading
import time
from loguru import logger
from typing import Dict, Optional, Callable

from app.tools import BuiltinTools, ToolResult
from app.utils.file_operation_recorder import (
    FileOperationRecorder,
)


class ToolExecutor:
    """工具执行器 - 统一调度各种工具"""

    # 需要记录的文件操作
    _FILE_OPS_TO_TRACK = {
        "write", "edit", "multiedit", "patch"
    }

    def __init__(self, homepage=None, workdir: str = None):
        self._homepage = homepage
        self._builtin_tools: Optional[BuiltinTools] = None
        self._workdir = workdir
        self._custom_tools: Dict[str, Callable] = {}
        self._session_id: Optional[str] = None
        self._call_id: Optional[str] = None

        # 文件操作记录器
        self._file_recorder: Optional[FileOperationRecorder] = None
        
        # 有效标志：用于检查 ToolExecutor 是否仍然有效（UI 未关闭）
        self._valid: bool = True

        self._initialize_builtin_tools()

    def _initialize_builtin_tools(self):
        """初始化内置工具"""
        import os

        workdir = self._workdir
        try:
            from app.utils.utils import resource_path

            workdir = resource_path("")
        except Exception:
            workdir = os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            )

        logger.info(f"[ToolExecutor] Initialized with workdir: {workdir}")
        self._builtin_tools = BuiltinTools(self._homepage, workdir)

    @property
    def builtin_tools(self) -> Optional[BuiltinTools]:
        return self._builtin_tools

    def is_valid(self) -> bool:
        """检查 ToolExecutor 是否仍然有效（UI 未关闭）"""
        if self._builtin_tools is None:
            return False
        return self._valid

    @property
    def todo_list(self):
        """获取待办事项列表"""
        if self._builtin_tools:
            return self._builtin_tools.todo_list
        return []

    def clear_todo_list(self):
        """清空待办事项列表"""
        if self._builtin_tools:
            self._builtin_tools.todo_clear()

    def reset_session_state(self):
        """Reset session-scoped state when switching sessions"""
        if self._builtin_tools:
            self._builtin_tools.reset_session_state()

    def cleanup(self):
        """
        彻底清理 ToolExecutor 的所有缓存，防止内存泄漏。
        应该在对话结束后或切换会话时调用。
        """
        # 标记为无效
        self._valid = False
        
        # 清理 builtin_tools
        if self._builtin_tools:
            try:
                self._builtin_tools.cleanup()
            except Exception as e:
                logger.warning(f"[ToolExecutor] Failed to cleanup builtin_tools: {e}")
            self._builtin_tools = None
        
        # 清理文件操作记录器
        if self._file_recorder:
            self._file_recorder = None
        
        # 清理会话上下文
        self._session_id = None
        self._call_id = None

    def _init_file_recorder(self):
        """初始化文件操作记录器"""
        if self._file_recorder is None:
            self._file_recorder = FileOperationRecorder()

    def set_session_context(self, session_id: str, call_id: str = None):
        """
        设置会话上下文（用于文件操作记录）

        Args:
            session_id: 会话 ID
            call_id: 当前调用 ID（可选）
        """
        self._session_id = session_id
        self._call_id = call_id
        self._init_file_recorder()

    def set_call_id(self, call_id: str):
        """设置当前调用 ID"""
        self._call_id = call_id

    @property
    def file_recorder(self) -> Optional[FileOperationRecorder]:
        """获取文件操作记录器"""
        return self._file_recorder

    def _record_file_operation_before(self, tool_name: str, args: dict):
        """
        在文件操作执行前记录备份信息

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            str: 文件完整路径，用于后续的编辑后备份
        """
        if tool_name not in self._FILE_OPS_TO_TRACK:
            return None

        if not self._session_id:
            return None

        if not self._call_id:
            return None

        if not self._file_recorder:
            return None

        # 获取文件路径
        path = args.get("path")
        if not path:
            return None

        logger.debug(f"[ToolExecutor] 准备记录文件操作: tool={tool_name}, path={path}")

        # 处理 URL 格式的文件路径 (如 file:/D:/xxx 或 file:///D:/xxx)
        import re
        if path.startswith("file:"):
            # 移除 file: 前缀，处理单斜杠或双斜杠
            path = re.sub(r'^file:/{1,3}', '', path)


        # 获取完整的文件路径
        if hasattr(self._builtin_tools, "_file_tools"):
            full_path = self._builtin_tools._file_tools._resolve_path(path)
        else:
            from pathlib import Path
            full_path = Path(path).resolve()



        # 记录操作（内部会处理文件不存在的情况）
        try:
            backup_path = self._file_recorder.record_operation(
                session_id=self._session_id,
                call_id=self._call_id,
                tool_name=tool_name,
                file_path=str(full_path)
            )
            if backup_path:
                logger.info(f"[FileRecorder] 已备份: {full_path} -> {backup_path}")
            else:
                # 文件不存在（如新建文件 write_file），跳过记录是正常的
                logger.debug(f"[ToolExecutor] 文件操作记录返回 None")
        except Exception as e:
            # 记录失败不阻塞工具执行
            logger.warning(f"[ToolExecutor] 记录文件操作失败: {e}")

        return str(full_path)

    def _record_file_operation_after(self, tool_name: str, args: dict, file_path_before: str):
        """
        在文件操作执行成功后备份编辑后的文件

        Args:
            tool_name: 工具名称
            args: 工具参数
            file_path_before: 执行前的文件路径
        """
        if not file_path_before:
            return

        if tool_name not in self._FILE_OPS_TO_TRACK:
            return

        if not self._session_id or not self._call_id:
            return

        if not self._file_recorder:
            return

        try:
            self._file_recorder.record_after_operation(
                session_id=self._session_id,
                call_id=self._call_id,
                tool_name=tool_name,
                file_path=file_path_before
            )
        except Exception as e:
            # 记录失败不阻塞主流程
            logger.warning(f"[ToolExecutor] 编辑后备份失败: {e}")

    def set_memory_manager(self, memory_manager):
        if self._builtin_tools:
            self._builtin_tools.set_memory_manager(memory_manager)
            logger.info("[ToolExecutor] MemoryManager attached to BuiltinTools")

    def set_llm_config_getter(self, getter: Callable):
        if self._builtin_tools:
            self._builtin_tools.set_llm_config_getter(getter)
            logger.info("[ToolExecutor] LLM config getter attached to BuiltinTools")

    def set_session_messages_getter(self, getter: Callable):
        if self._builtin_tools:
            self._builtin_tools.set_session_messages_getter(getter)
            logger.info(
                "[ToolExecutor] Session messages getter attached to BuiltinTools"
            )

    def set_agent_manager(self, agent_manager):
        """设置 AgentManager 实例，用于动态生成工具 schema"""
        if self._builtin_tools:
            self._builtin_tools.set_agent_manager(agent_manager)
            logger.info("[ToolExecutor] AgentManager attached to BuiltinTools")

    # 工具必需参数定义
    REQUIRED_ARGS = {
        "read": ["path"],
        "write": ["path", "content"],
        "edit": ["path", "oldString", "newString"],
        "multiedit": ["path", "edits"],
        "grep": ["pattern"],
        "glob": ["pattern"],
        "patch": ["path", "patch_content"],
        "bash": ["command"],
        "webfetch": ["url"],
        "websearch": ["query"],
        "scan_repo": [],
        "stage_files": ["files"],
        "git_status": [],
        "git_log": [],
        "git_diff": [],
        "get_diagnostics": ["file_path"],
        "summarize_changes": ["text"],
        "memory_list": [],
        "memory_search": ["query"],
        "memory_save": ["content"],
        "memory_consolidate": [],
        "todowrite": ["todos"],
        "todoread": [],
        "task_batch": ["tasks"],
        "task_wait": ["task_ids"],
        "task_status": [],
        "skill": ["name"],
        "list_skills": [],
        "question": ["question"],
        "list_webhooks": [],
        "trigger_webhook": ["endpoint"],
    }

    def execute(self, tool_name: str, args: dict, cancelled_ref: list = None) -> ToolResult:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]

        Returns:
            ToolResult: 执行结果
        """
        # 检查 ToolExecutor 是否仍然有效（API 模式下 UI 可能已关闭）
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor is invalid (UI may be closed)")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        logger.info(f"[ToolExecutor] Executing tool: {tool_name}, args: {args}")

        # 校验必需参数
        if tool_name in self.REQUIRED_ARGS:
            required = self.REQUIRED_ARGS[tool_name]
            missing = [p for p in required if not args.get(p)]
            if missing:
                return ToolResult(False, error=f"Missing required arguments: {missing}")

        # 对于耗时工具（如 grep, bash, webfetch, websearch），使用异步执行
        if tool_name == "grep":
            return self._execute_grep_async(args, cancelled_ref)
        elif tool_name == "webfetch":
            return self._execute_webfetch_async(args, cancelled_ref)
        elif tool_name == "websearch":
            return self._execute_websearch_async(args, cancelled_ref)

        # 文件操作前记录（用于撤销）
        file_path_before = self._record_file_operation_before(tool_name, args)

        if tool_name in self._custom_tools:
            try:
                result = self._custom_tools[tool_name](args)
                return ToolResult(True, content=result)
            except Exception as e:
                return ToolResult(False, error=f"Custom tool error: {str(e)}")

        tool_map = {
            "read": lambda: self._builtin_tools.read_file(
                path=args.get("path"),  # 统一使用 path
                offset=args.get("offset", 1),
                limit=args.get("limit", 500),  # 建议默认值设为 500，防止 Token 溢出
                show_line_numbers=args.get("show_line_numbers", False),
            ),
            "write": lambda: self._builtin_tools.write_file(
                path=args.get("path"), content=args.get("content", "")
            ),
            "edit": lambda: self._builtin_tools.edit_file(
                path=args.get("path"),
                oldString=args.get("oldString", ""),
                newString=args.get("newString", ""),
                replaceAll=args.get("replaceAll", False),
            ),
            "multiedit": lambda: self._builtin_tools.multi_edit(
                path=args.get("path"),
                edits=args.get("edits", []),
            ),
            "grep": lambda: self._builtin_tools.grep_files(
                pattern=args.get("pattern"),
                path=args.get("path", ""),  # 默认当前路径
                include=args.get("include"),
            ),
            "glob": lambda: self._builtin_tools.glob_files(
                pattern=args.get("pattern"),
                path=args.get("path", ""),  # 默认当前路径
            ),
            "list": lambda: self._builtin_tools.list_directory(
                path=args.get("path", "")  # 默认当前路径
            ),
            "patch": lambda: self._builtin_tools.apply_patch(
                args.get("path"), args.get("patch_content", "")
            ),
            "git_status": lambda: self._builtin_tools.git_status(args.get("path")),
            "git_log": lambda: self._builtin_tools.git_log(
                args.get("path"), args.get("max_count", 10)
            ),
            "git_diff": lambda: self._builtin_tools.git_diff(
                args.get("ref1"), args.get("ref2"), args.get("path")
            ),
            "bash": lambda: self._builtin_tools.execute_bash(
                args.get("command", ""), args.get("timeout", 120)
            ),
            "webfetch": lambda: self._builtin_tools.fetch_web(
                args.get("url", ""), args.get("format", "markdown")
            ),
            "websearch": lambda: self._builtin_tools.search_web(
                args.get("query", ""), args.get("num_results", 10)
            ),
            "scan_repo": lambda: self._builtin_tools.scan_repo(
                args.get("path"), args.get("max_depth", 2)
            ),
            "stage_files": lambda: self._builtin_tools.stage_files(
                args.get("files", [])
            ),
            "get_diagnostics": lambda: self._builtin_tools.get_diagnostics(
                args.get("file_path", ""), args.get("language")
            ),
            "summarize_changes": lambda: self._builtin_tools.summarize_changes(
                args.get("text", ""), args.get("limit", 1200)
            ),
            "memory_list": lambda: self._builtin_tools.memory_list(
                args.get("limit", 10),
                args.get("include_disabled", False),
            ),
            "memory_search": lambda: self._builtin_tools.memory_search(
                args.get("query", ""),
                args.get("limit", 8),
                args.get("include_disabled", False),
            ),
            "memory_save": lambda: self._builtin_tools.memory_save(
                args.get("content", ""),
                args.get("confidence", 0.8),
                args.get("source", "assistant"),
                args.get("conflict_group", ""),
            ),
            "memory_consolidate": lambda: self._builtin_tools.memory_consolidate(
                args.get("max_items", 3),
                args.get("save", True),
            ),
            "todowrite": lambda: self._builtin_tools.todo_write(args.get("todos", [])),
            "todoread": lambda: self._builtin_tools.todo_read(),
            "task_batch": lambda: self._builtin_tools.task_execute_batch(
                args.get("tasks", [])
            ),
            "task_status": lambda: self._builtin_tools.task_status(
                args.get("task_ids"),
                args.get("with_log", False),
                args.get("with_result", True),
            ),
            "skill": lambda: self._builtin_tools.load_skill(args.get("name", "")),
            "list_skills": lambda: self._builtin_tools.list_skills(),
            "question": lambda: self._builtin_tools.ask_question(
                args.get("question", ""),
                args.get("options"),
                args.get("multiple", False),
            ),
            "list_webhooks": lambda: self._builtin_tools.list_canvases(),
            "trigger_webhook": lambda: self._builtin_tools.trigger_canvas(
                args.get("endpoint", ""),
                args.get("data"),
                args.get("callback_url"),
                args.get("timeout", 300),
            ),
        }

        executor = tool_map.get(tool_name)
        if executor:
            try:
                result = executor()
                # 文件操作成功后备份编辑后的文件（用于差异对比）
                if tool_name in self._FILE_OPS_TO_TRACK and result and result.success:
                    self._record_file_operation_after(tool_name, args, file_path_before)
                return result
            except Exception as e:
                return ToolResult(False, error=f"Execution error: {str(e)}")

        if self._canvas_tools_executor and tool_name.startswith("canvas_"):
            return self._execute_canvas_tool(tool_name, args)

        return ToolResult(False, error=f"Unknown tool: {tool_name}")

    def _execute_grep_async(self, args: dict, cancelled_ref: list = None) -> ToolResult:
        """
        异步执行 grep，使用子线程，完成后返回结果
        
        Args:
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]
        
        Returns:
            ToolResult: 执行结果
        """
        if not self._builtin_tools or not self._builtin_tools._file_tools:
            return ToolResult(False, error="FileTools not available")
        
        pattern = args.get("pattern", "")
        path = args.get("path", "")
        include = args.get("include")
        
        # 使用 threading.Event 进行同步
        result_holder = [None]
        finished = threading.Event()
        
        def on_grep_done(result):
            result_holder[0] = result
            finished.set()
        
        # 启动异步 grep
        self._builtin_tools._file_tools.grep_files(
            pattern=pattern,
            path=path,
            include=include,
            callback=on_grep_done
        )
        
        # 等待完成，支持取消
        while not finished.is_set():
            # 检查取消标志
            if cancelled_ref is not None and cancelled_ref[0]:
                self._builtin_tools._file_tools.cancel()
                result_holder[0] = ToolResult(False, error="用户中止")
                finished.set()
                break
            time.sleep(0.05)
        
        return result_holder[0] if result_holder[0] else ToolResult(False, error="Grep failed")

    def _execute_webfetch_async(self, args: dict, cancelled_ref: list = None) -> ToolResult:
        """
        异步执行网页抓取
        
        Args:
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]
        
        Returns:
            ToolResult: 执行结果
        """
        if not self._builtin_tools or not self._builtin_tools._web_tools:
            return ToolResult(False, error="WebTools not available")
        
        url = args.get("url", "")
        format = args.get("format", "markdown")
        max_chars = args.get("max_chars", 26000)
        
        # 使用 threading.Event 进行同步
        result_holder = [None]
        finished = threading.Event()
        
        def on_fetch_done(result):
            result_holder[0] = result
            finished.set()
        
        self._builtin_tools._web_tools.fetch_web(
            url=url, format=format, max_chars=max_chars,
            callback=on_fetch_done, cancelled_ref=cancelled_ref
        )
        
        # 等待完成，支持取消
        while not finished.is_set():
            if cancelled_ref is not None and cancelled_ref[0]:
                result_holder[0] = ToolResult(False, error="用户中止")
                finished.set()
                break
            time.sleep(0.05)
        
        return result_holder[0] if result_holder[0] else ToolResult(False, error="WebFetch failed")

    def _execute_websearch_async(self, args: dict, cancelled_ref: list = None) -> ToolResult:
        """
        异步执行网络搜索
        
        Args:
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]
        
        Returns:
            ToolResult: 执行结果
        """
        if not self._builtin_tools or not self._builtin_tools._web_tools:
            return ToolResult(False, error="WebTools not available")
        
        query = args.get("query", "")
        num_results = args.get("num_results", 10)
        
        # 使用 threading.Event 进行同步
        result_holder = [None]
        finished = threading.Event()
        
        def on_search_done(result):
            result_holder[0] = result
            finished.set()
        
        self._builtin_tools._web_tools.search_web(
            query=query, num_results=num_results,
            callback=on_search_done, cancelled_ref=cancelled_ref
        )
        
        # 等待完成，支持取消
        while not finished.is_set():
            if cancelled_ref is not None and cancelled_ref[0]:
                result_holder[0] = ToolResult(False, error="用户中止")
                finished.set()
                break
            time.sleep(0.05)
        
        return result_holder[0] if result_holder[0] else ToolResult(False, error="WebSearch failed")

    def set_sub_agent_manager(self, sub_agent_manager):
        """设置子智能体管理器"""
        if self._builtin_tools:
            self._builtin_tools._sub_agent_manager = sub_agent_manager
            self._builtin_tools._task_tools._sub_agent_manager = sub_agent_manager
            logger.info(
                "[ToolExecutor] SubAgentManager attached to BuiltinTools and TaskTools"
            )
