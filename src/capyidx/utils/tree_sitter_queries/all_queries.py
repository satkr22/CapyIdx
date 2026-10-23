
LANGUAGE_QUERIES = {

"python": """

(class_definition
  name: (identifier) @name.definition.class) @definition.class

(function_definition
  name: (identifier) @name.definition.function) @definition.function

(decorated_definition
  (function_definition
    name: (identifier) @name.definition.function) @definition.function
) @definition.decorator

(call
  function: [
      (identifier) @name.reference.call
      (attribute
        attribute: (identifier) @name.reference.call)
  ]) @reference.call



""",


"typescript": """


; Exported function declarations
(export_statement
  declaration: (function_declaration
    name: (identifier) @name.definition.function)) @definition.function

; Exported class declarations
(export_statement
  declaration: (class_declaration
    name: (type_identifier) @name.definition.class)) @definition.class

; Exported interfaces
(export_statement
  declaration: (interface_declaration
    name: (type_identifier) @name.definition.interface)) @definition.interface

; Exported type aliases
(export_statement
  declaration: (type_alias_declaration
    name: (type_identifier) @name.definition.type)) @definition.type

; Exported enums
(export_statement
  declaration: (enum_declaration
    name: (identifier) @name.definition.enum)) @definition.enum

; Exported arrow functions and function expressions
(export_statement
  declaration: (lexical_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function_expression)]))) @definition.function

; Exported var declarations, if applicable
(export_statement
  declaration: (variable_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function_expression)]))) @definition.function
    
    
    
""",




"javascript": """


(
  (comment)* @doc
  .
  (method_definition
    name: (property_identifier) @name.definition.method) @definition.method
  (#not-eq? @name.definition.method "constructor")
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.method)
)

(
  (comment)* @doc
  .
  [
    (class
      name: (_) @name.definition.class)
    (class_declaration
      name: (_) @name.definition.class)
  ] @definition.class
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.class)
)

(
  (comment)* @doc
  .
  [
    (function
      name: (identifier) @name.definition.function)
    (function_declaration
      name: (identifier) @name.definition.function)
    (generator_function
      name: (identifier) @name.definition.function)
    (generator_function_declaration
      name: (identifier) @name.definition.function)
  ] @definition.function
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function)]) @definition.function)
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (variable_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function)]) @definition.function)
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(assignment_expression
  left: [
    (identifier) @name.definition.function
    (member_expression
      property: (property_identifier) @name.definition.function)
  ]
  right: [(arrow_function) (function)]
) @definition.function

(pair
  key: (property_identifier) @name.definition.function
  value: [(arrow_function) (function)]) @definition.function

(
  (call_expression
    function: (identifier) @name.reference.call) @reference.call
  (#not-match? @name.reference.call "^(require)$")
)

(call_expression
  function: (member_expression
    property: (property_identifier) @name.reference.call)
  arguments: (_) @reference.call)

(new_expression
  constructor: (_) @name.reference.class) @reference.class



""",




"java": """


(class_declaration
  name: (identifier) @name.definition.class) @definition.class

(method_declaration
  name: (identifier) @name.definition.method) @definition.method

(method_invocation
  name: (identifier) @name.reference.call
  arguments: (argument_list) @reference.call)

(interface_declaration
  name: (identifier) @name.definition.interface) @definition.interface

(type_list
  (type_identifier) @name.reference.implementation) @reference.implementation

(object_creation_expression
  type: (type_identifier) @name.reference.class) @reference.class

(superclass (type_identifier) @name.reference.class) @reference.class



""",




"rust": """


; ADT definitions

(struct_item
    name: (type_identifier) @name.definition.class) @definition.class

(enum_item
    name: (type_identifier) @name.definition.class) @definition.class

(union_item
    name: (type_identifier) @name.definition.class) @definition.class

; type aliases

(type_item
    name: (type_identifier) @name.definition.class) @definition.class

; method definitions

(declaration_list
    (function_item
        name: (identifier) @name.definition.method)) @definition.method

; function definitions

(function_item
    name: (identifier) @name.definition.function) @definition.function

; trait definitions
(trait_item
    name: (type_identifier) @name.definition.interface) @definition.interface

; module definitions
(mod_item
    name: (identifier) @name.definition.module) @definition.module

; macro definitions

(macro_definition
    name: (identifier) @name.definition.macro) @definition.macro

; references

(call_expression
    function: (identifier) @name.reference.call) @reference.call

(call_expression
    function: (field_expression
        field: (field_identifier) @name.reference.call)) @reference.call

(macro_invocation
    macro: (identifier) @name.reference.call) @reference.call

; implementations

(impl_item
    trait: (type_identifier) @name.reference.implementation) @reference.implementation

(impl_item
    type: (type_identifier) @name.reference.implementation
    !trait) @reference.implementation



""",




"ruby": """


; Method definitions

(
  (comment)* @doc
  .
  [
    (method
      name: (_) @name.definition.method) @definition.method
    (singleton_method
      name: (_) @name.definition.method) @definition.method
  ]
  (#strip! @doc "^#\\s*")
  (#select-adjacent! @doc @definition.method)
)

(alias
  name: (_) @name.definition.method) @definition.method

(setter
  (identifier) @ignore)

; Class definitions

(
  (comment)* @doc
  .
  [
    (class
      name: [
        (constant) @name.definition.class
        (scope_resolution
          name: (_) @name.definition.class)
      ]) @definition.class
    (singleton_class
      value: [
        (constant) @name.definition.class
        (scope_resolution
          name: (_) @name.definition.class)
      ]) @definition.class
  ]
  (#strip! @doc "^#\\s*")
  (#select-adjacent! @doc @definition.class)
)

; Module definitions

(
  (module
    name: [
      (constant) @name.definition.module
      (scope_resolution
        name: (_) @name.definition.module)
    ]) @definition.module
)

; Calls

(call method: (identifier) @name.reference.call) @reference.call

(
  [(identifier) (constant)] @name.reference.call @reference.call
  (#is-not? local)
  (#not-match? @name.reference.call "^(lambda|load|require|require_relative|__FILE__|__LINE__)$")
)



""",




"c_sharp": """

(class_declaration
 name: (identifier) @name.definition.class
 ) @definition.class

(class_declaration
   bases: (base_list (_) @name.reference.class)
 ) @reference.class

(interface_declaration
 name: (identifier) @name.definition.interface
 ) @definition.interface

(interface_declaration
 bases: (base_list (_) @name.reference.interface)
 ) @reference.interface

(method_declaration
 name: (identifier) @name.definition.method
 ) @definition.method

(object_creation_expression
 type: (identifier) @name.reference.class
 ) @reference.class

(type_parameter_constraints_clause
 target: (identifier) @name.reference.class
 ) @reference.class

(type_constraint
 type: (identifier) @name.reference.class
 ) @reference.class

(variable_declaration
 type: (identifier) @name.reference.class
 ) @reference.class

(invocation_expression
 function:
  (member_access_expression
    name: (identifier) @name.reference.send
 )
) @reference.send

(namespace_declaration
 name: (identifier) @name.definition.module
) @definition.module




""",




"c": """


(struct_specifier name: (type_identifier) @name.definition.class body:(_)) @definition.class

(declaration type: (union_specifier name: (type_identifier) @name.definition.class)) @definition.class

(function_declarator declarator: (identifier) @name.definition.function) @definition.function

(type_definition declarator: (type_identifier) @name.definition.type) @definition.type

(enum_specifier name: (type_identifier) @name.definition.type) @definition.type



""",




"cpp": """


(struct_specifier name: (type_identifier) @name.definition.class body:(_)) @definition.class

(declaration type: (union_specifier name: (type_identifier) @name.definition.class)) @definition.class

(function_declarator declarator: (identifier) @name.definition.function) @definition.function

(function_declarator declarator: (field_identifier) @name.definition.function) @definition.function

(function_declarator declarator: (qualified_identifier scope: (namespace_identifier) @scope name: (identifier) @name.definition.method)) @definition.method

(type_definition declarator: (type_identifier) @name.definition.type) @definition.type

(enum_specifier name: (type_identifier) @name.definition.type) @definition.type

(class_specifier name: (type_identifier) @name.definition.class) @definition.class



""",




"elisp": """


;; defun/defsubst
(function_definition name: (symbol) @name.definition.function) @definition.function

;; Treat macros as function definitions for the sake of TAGS.
(macro_definition name: (symbol) @name.definition.function) @definition.function

;; Match function calls
(list (symbol) @name.reference.function) @reference.function



""",




"elixier": """

; Definitions

; * modules and protocols
(call
  target: (identifier) @ignore
  (arguments (alias) @name.definition.module)
  (#match? @ignore "^(defmodule|defprotocol)$")) @definition.module

; * functions/macros
(call
  target: (identifier) @ignore
  (arguments
    [
      ; zero-arity functions with no parentheses
      (identifier) @name.definition.function
      ; regular function clause
      (call target: (identifier) @name.definition.function)
      ; function clause with a guard clause
      (binary_operator
        left: (call target: (identifier) @name.definition.function)
        operator: "when")
    ])
  (#match? @ignore "^(def|defp|defdelegate|defguard|defguardp|defmacro|defmacrop|defn|defnp)$")) @definition.function

; References

; ignore calls to kernel/special-forms keywords
(call
  target: (identifier) @ignore
  (#match? @ignore "^(def|defp|defdelegate|defguard|defguardp|defmacro|defmacrop|defn|defnp|defmodule|defprotocol|defimpl|defstruct|defexception|defoverridable|alias|case|cond|else|for|if|import|quote|raise|receive|require|reraise|super|throw|try|unless|unquote|unquote_splicing|use|with)$"))

; ignore module attributes
(unary_operator
  operator: "@"
  operand: (call
    target: (identifier) @ignore))

; * function call
(call
  target: [
   ; local
   (identifier) @name.reference.call
   ; remote
   (dot
     right: (identifier) @name.reference.call)
  ]) @reference.call

; * pipe into function call
(binary_operator
  operator: "|>"
  right: (identifier) @name.reference.call) @reference.call

; * modules
(alias) @name.reference.module @reference.module




""",




"elm": """


(value_declaration (function_declaration_left (lower_case_identifier) @name.definition.function)) @definition.function

(function_call_expr (value_expr (value_qid) @name.reference.function)) @reference.function
(exposed_value (lower_case_identifier) @name.reference.function)) @reference.function
(type_annotation ((lower_case_identifier) @name.reference.function) (colon)) @reference.function

(type_declaration ((upper_case_identifier) @name.definition.type) ) @definition.type

(type_ref (upper_case_qid (upper_case_identifier) @name.reference.type)) @reference.type
(exposed_type (upper_case_identifier) @name.reference.type)) @reference.type

(type_declaration (union_variant (upper_case_identifier) @name.definition.union)) @definition.union

(value_expr (upper_case_qid (upper_case_identifier) @name.reference.union)) @reference.union


(module_declaration 
    (upper_case_qid (upper_case_identifier)) @name.definition.module
) @definition.module



""",




"go": """


(
  (comment)* @doc
  .
  (function_declaration
    name: (identifier) @name.definition.function) @definition.function
  (#strip! @doc "^//\\s*")
  (#set-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (method_declaration
    name: (field_identifier) @name.definition.method) @definition.method
  (#strip! @doc "^//\\s*")
  (#set-adjacent! @doc @definition.method)
)

(call_expression
  function: [
    (identifier) @name.reference.call
    (parenthesized_expression (identifier) @name.reference.call)
    (selector_expression field: (field_identifier) @name.reference.call)
    (parenthesized_expression (selector_expression field: (field_identifier) @name.reference.call))
  ]) @reference.call

(type_spec
  name: (type_identifier) @name.definition.type) @definition.type

(type_identifier) @name.reference.type @reference.type



""",




"ocaml": """

; Modules
;--------

(
  (comment)? @doc .
  (module_definition (module_binding (module_name) @name.definition.module) @definition.module)
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

(module_path (module_name) @name.reference.module) @reference.module

; Modules types
;--------------

(
  (comment)? @doc .
  (module_type_definition (module_type_name) @name.definition.interface) @definition.interface
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

(module_type_path (module_type_name) @name.reference.implementation) @reference.implementation

; Functions
;----------

(
  (comment)? @doc .
  (value_definition
    [
      (let_binding
        pattern: (value_name) @name.definition.function
        (parameter))
      (let_binding
        pattern: (value_name) @name.definition.function
        body: [(fun_expression) (function_expression)])
    ] @definition.function
  )
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

(
  (comment)? @doc .
  (external (value_name) @name.definition.function) @definition.function
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

(application_expression
  function: (value_path (value_name) @name.reference.call)) @reference.call

(infix_expression
  left: (value_path (value_name) @name.reference.call)
  (infix_operator) @reference.call
  (#eq? @reference.call "@@"))

(infix_expression
  (infix_operator) @reference.call
  right: (value_path (value_name) @name.reference.call)
  (#eq? @reference.call "|>"))

; Operator
;---------

(
  (comment)? @doc .
  (value_definition
    (let_binding
      pattern: (parenthesized_operator [
        (prefix_operator)
        (infix_operator)
        (hash_operator)
        (indexing_operator)
        (let_operator)
        (and_operator)
        (match_operator)
      ] @name.definition.function)) @definition.function)
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

[
  (prefix_operator)
  (sign_operator)
  (infix_operator)
  (hash_operator)
  (indexing_operator)
  (let_operator)
  (and_operator)
  (match_operator)
] @name.reference.call @reference.call

; Classes
;--------

(
  (comment)? @doc .
  [
    (class_definition (class_binding (class_name) @name.definition.class) @definition.class)
    (class_type_definition (class_type_binding (class_type_name) @name.definition.class) @definition.class)
  ]
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

[
  (class_path (class_name) @name.reference.class)
  (class_type_path (class_type_name) @name.reference.class)
] @reference.class

; Methods
;--------

(
  (comment)? @doc .
  (method_definition (method_name) @name.definition.method) @definition.method
  (#strip! @doc "^\\(\\*\\*?\\s*|\\s\\*\\)$")
)

(method_invocation (method_name) @name.reference.call) @reference.call




""",




"php": """


(class_declaration
  name: (name) @name.definition.class) @definition.class

(function_definition
  name: (name) @name.definition.function) @definition.function

(method_declaration
  name: (name) @name.definition.function) @definition.function

(object_creation_expression
  [
    (qualified_name (name) @name.reference.class)
    (variable_name (name) @name.reference.class)
  ]) @reference.class

(function_call_expression
  function: [
    (qualified_name (name) @name.reference.call)
    (variable_name (name)) @name.reference.call
  ]) @reference.call

(scoped_call_expression
  name: (name) @name.reference.call) @reference.call

(member_call_expression
  name: (name) @name.reference.call) @reference.call



""",




"ql": """



(classlessPredicate
  name: (predicateName) @name.definition.function) @definition.function

(memberPredicate
  name: (predicateName) @name.definition.method) @definition.method

(aritylessPredicateExpr
  name: (literalId) @name.reference.call) @reference.call

(module
  name: (moduleName) @name.definition.module) @definition.module

(dataclass
  name: (className) @name.definition.class) @definition.class

(datatype
  name: (className) @name.definition.class) @definition.class

(datatypeBranch
  name: (className) @name.definition.class) @definition.class

(qualifiedRhs
  name: (predicateName) @name.reference.call) @reference.call

(typeExpr
  name: (className) @name.reference.type) @reference.type


""",




}

